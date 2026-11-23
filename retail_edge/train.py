"""Train, evaluate and version the demand model.

    python -m retail_edge.train [--trees 300] [--quick]

Time-based split (no shuffling, no leakage): everything up to the cutoff
trains the model, everything after it is the hold-out test set that the
live simulator later replays.  Rows whose 7-day target window crosses the
cutoff are dropped from training (purged gap).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.tree import DecisionTreeRegressor

from . import data as data_mod
from .config import settings
from .features import FEATURES, HORIZON, build_training_frame

log = logging.getLogger(__name__)
CUTOFF = pd.Timestamp("2015-11-22")   # last ~6 months held out


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    y, p = np.asarray(y, float), np.asarray(p, float)
    nz = y > 0
    return {
        "mae": round(float(mean_absolute_error(y, p)), 3),
        "rmse": round(float(np.sqrt(mean_squared_error(y, p))), 3),
        "r2": round(float(r2_score(y, p)), 4),
        "wape": round(float(np.abs(y - p).sum() / max(y.sum(), 1e-9)), 4),
        "mape_nonzero": round(float(np.mean(np.abs(y[nz] - p[nz]) / y[nz])), 4) if nz.any() else None,
        "n": int(len(y)),
    }


def split(frame: pd.DataFrame):
    dates = np.sort(frame["date"].unique())
    cut_idx = np.searchsorted(dates, CUTOFF.to_datetime64())
    purge_from = dates[max(cut_idx - HORIZON, 0)]
    train = frame[frame["date"] < purge_from]
    test = frame[(frame["date"] >= dates[cut_idx]) & frame["target"].notna()]
    return train, test


def cv_select(train: pd.DataFrame, candidates: list[dict], make, seed: int) -> tuple[dict, list]:
    """Pick hyper-parameters with expanding-window time-series CV."""
    days = np.sort(train["date"].unique())
    tss = TimeSeriesSplit(n_splits=3)
    results = []
    for params in candidates:
        maes = []
        for tr_idx, va_idx in tss.split(days):
            tr = train[train["date"].isin(days[tr_idx[:-HORIZON]])]
            va = train[train["date"].isin(days[va_idx])]
            m = make(params, seed).fit(tr[FEATURES], tr["target"])
            maes.append(mean_absolute_error(va["target"], m.predict(va[FEATURES])))
        results.append({"params": params, "cv_mae": round(float(np.mean(maes)), 3)})
    best = min(results, key=lambda r: r["cv_mae"])
    return best["params"], results


def make_rf(p, seed):
    return RandomForestRegressor(random_state=seed, n_jobs=-1, **p)


def make_dt(p, seed):
    return DecisionTreeRegressor(random_state=seed, **p)


def train(trees: int = 200, quick: bool = False, out_dir: Path | None = None) -> Path:
    seed = settings.seed
    np.random.seed(seed)
    daily = data_mod.load_daily()
    catalog = data_mod.load_catalog()
    frame = build_training_frame(daily)
    tr, te = split(frame)
    log.info("train rows=%d  test rows=%d", len(tr), len(te))

    rf_grid = [{"n_estimators": 60 if quick else 120, "max_depth": d, "min_samples_leaf": l,
                "max_features": 0.5} for d in (16, None) for l in (5, 15)]
    dt_grid = [{"max_depth": d, "min_samples_leaf": l} for d in (8, 10, 14) for l in (10, 30)]
    if quick:
        rf_grid, dt_grid = rf_grid[:1], dt_grid[:2]
    rf_params, rf_cv = cv_select(tr, rf_grid, make_rf, seed)
    dt_params, dt_cv = cv_select(tr, dt_grid, make_dt, seed)
    rf_params = {**rf_params, "n_estimators": 40 if quick else trees}

    t0 = time.perf_counter()
    rf = make_rf(rf_params, seed).fit(tr[FEATURES], tr["target"])
    rf_fit_s = time.perf_counter() - t0
    dtree = make_dt(dt_params, seed).fit(tr[FEATURES], tr["target"])

    X_te, y_te = te[FEATURES], te["target"].to_numpy()
    preds = {
        "random_forest": rf.predict(X_te),
        "decision_tree": dtree.predict(X_te),
        "naive_last_7": (te["roll_7_mean"] * HORIZON).to_numpy(),
        "moving_avg_28": (te["roll_28_mean"] * HORIZON).to_numpy(),
    }
    results = {k: metrics(y_te, v) for k, v in preds.items()}
    results_train = {"random_forest": metrics(tr["target"], rf.predict(tr[FEATURES])),
                     "decision_tree": metrics(tr["target"], dtree.predict(tr[FEATURES]))}

    # per-SKU residual std (daily scale) -> safety stock at the edge
    res = te.assign(err=y_te - preds["random_forest"])
    resid_std = (res.groupby("sku")["err"].std() / np.sqrt(HORIZON)).round(3).to_dict()
    cat_names = catalog.set_index("sku")["dept_id"]
    te_rf = pd.Series(preds["random_forest"], index=te.index)
    per_cat = {c: metrics(g["target"], te_rf.loc[g.index])
               for c, g in te.assign(cat=te["sku"].map(cat_names)).groupby("cat")}

    # single-row latency on this machine (what one edge request costs)
    rf.set_params(n_jobs=1)
    one = X_te.iloc[[0]]
    for _ in range(5):
        rf.predict(one)
    lat = []
    for i in range(50):
        t = time.perf_counter(); rf.predict(X_te.iloc[[i % len(X_te)]]); lat.append((time.perf_counter() - t) * 1e3)

    version = dt.datetime.now().strftime("v%Y%m%d-%H%M%S")
    out_dir = out_dir or settings.model_dir
    vdir = out_dir / version
    vdir.mkdir(parents=True, exist_ok=True)
    joblib.dump(rf, vdir / "model.joblib", compress=3)
    joblib.dump(dtree, vdir / "decision_tree.joblib", compress=3)
    data_hash = hashlib.sha256(data_mod.DAILY_FILE.read_bytes()).hexdigest()[:16] if data_mod.DAILY_FILE.exists() else None
    rng = np.random.default_rng(seed)
    sample_idx = rng.choice(len(te), size=min(600, len(te)), replace=False)
    meta = {
        "version": version,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "algorithm": "RandomForestRegressor",
        "target": f"units sold over next {HORIZON} trading days",
        "horizon_days": HORIZON,
        "features": FEATURES,
        "hyperparameters": {"random_forest": rf_params, "decision_tree": dt_params},
        "cv": {"random_forest": rf_cv, "decision_tree": dt_cv},
        "split": {"train_end": str(tr["date"].max().date()), "test_start": str(te["date"].min().date()),
                  "test_end": str(te["date"].max().date()), "train_rows": int(len(tr)), "test_rows": int(len(te)),
                  "purge_days": HORIZON},
        "metrics_test": results,
        "metrics_train": results_train,
        "metrics_by_category": per_cat,
        "feature_importance": dict(sorted(zip(FEATURES, map(lambda v: round(float(v), 4), rf.feature_importances_)),
                                          key=lambda kv: -kv[1])),
        "residual_std_daily": resid_std,
        "latency_ms_single_row": {"p50": round(float(np.percentile(lat, 50)), 2),
                                  "p95": round(float(np.percentile(lat, 95)), 2)},
        "fit_seconds": round(rf_fit_s, 2),
        "dataset": {"name": "M5 Forecasting (Walmart), store CA_1", "doi": "10.5281/zenodo.12636070", "sha256_16": data_hash,
                    "products": int(daily["sku"].nunique()), "days": int(daily["date"].nunique())},
        "test_sample": {"actual": [float(y_te[i]) for i in sample_idx],
                        "predicted": [round(float(preds["random_forest"][i]), 2) for i in sample_idx],
                        "decision_tree": [round(float(preds["decision_tree"][i]), 2) for i in sample_idx]},
        "seed": seed,
    }
    (vdir / "metadata.json").write_text(json.dumps(meta, indent=2))
    (out_dir / "LATEST").write_text(version)
    log.info("saved %s  RF test=%s", vdir, results["random_forest"])
    return vdir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trees", type=int, default=200)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    vdir = train(a.trees, a.quick)
    meta = json.loads((vdir / "metadata.json").read_text())
    print(json.dumps({"version": meta["version"], "test": meta["metrics_test"],
                      "importance": list(meta["feature_importance"].items())[:6],
                      "latency": meta["latency_ms_single_row"], "hp": meta["hyperparameters"]}, indent=1))


if __name__ == "__main__":
    main()
