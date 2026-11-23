"""Dataset acquisition and cleaning.

Source: M5 Forecasting (Walmart unit sales, University of Nicosia / Kaggle,
2020), mirrored openly on Zenodo, CC BY 4.0:
    https://doi.org/10.5281/zenodo.12636070
Daily unit sales for 3,049 products in 10 US stores, 2011-01-29 .. 2016-05-22,
with product hierarchy (category, department), weekly sell prices and an
event / SNAP calendar.

This project models ONE store (CA_1) as the edge node, the way a real
deployment would run one model per store on in-store hardware.
Pipeline: wide sales matrix -> long daily table -> cleaning -> selection of
the products the store sells consistently in every department.
"""
from __future__ import annotations

import json
import logging
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .config import settings

log = logging.getLogger(__name__)

M5_URL = "https://zenodo.org/records/12636070/files/m5-forecasting-accuracy.zip?download=1"
STORE = "CA_1"
START = "2013-01-01"            # last ~3.4 years: enough for 3 full seasonal cycles
PER_DEPT = 7                    # products per department -> 7 depts x 7 = 49 SKUs
DAILY_FILE = settings.data_dir / "daily_sales.csv.gz"
CATALOG_FILE = settings.data_dir / "catalog.csv"
CALENDAR_FILE = settings.data_dir / "calendar.csv"
CLEANING_REPORT = settings.data_dir / "cleaning_report.json"

CATEGORY_LABEL = {"FOODS": "Foods", "HOBBIES": "Hobbies", "HOUSEHOLD": "Household"}
DEPT_LABEL = {d: f"{CATEGORY_LABEL[d.rsplit('_', 1)[0]]} · Dept {d[-1]}" for d in
              ["FOODS_1", "FOODS_2", "FOODS_3", "HOBBIES_1", "HOBBIES_2", "HOUSEHOLD_1", "HOUSEHOLD_2"]}
# M5 anonymises departments and items, so the UI shows the real IDs.


def download_raw() -> Path:
    raw = settings.raw_dir
    need = ["calendar.csv", "sales_train_evaluation.csv", "sell_prices.csv"]
    if all((raw / n).exists() for n in need):
        return raw
    raw.mkdir(parents=True, exist_ok=True)
    z = raw / "m5.zip"
    if not z.exists() or not zipfile.is_zipfile(z):
        log.info("downloading %s (48 MB)", M5_URL)
        with urllib.request.urlopen(M5_URL, timeout=600) as r, open(z, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
    with zipfile.ZipFile(z) as zf:
        for n in need:
            zf.extract(n, raw)
    return raw


def _read_store_sales(raw: Path) -> pd.DataFrame:
    parts = [c[c["store_id"] == STORE] for c in
             pd.read_csv(raw / "sales_train_evaluation.csv", chunksize=3000)]
    return pd.concat(parts, ignore_index=True)


def _read_store_prices(raw: Path, items: set[str]) -> pd.DataFrame:
    parts = []
    for c in pd.read_csv(raw / "sell_prices.csv", chunksize=500_000):
        parts.append(c[(c["store_id"] == STORE) & c["item_id"].isin(items)])
    return pd.concat(parts, ignore_index=True)


def prepare() -> pd.DataFrame:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    raw = download_raw()
    cal = pd.read_csv(raw / "calendar.csv", parse_dates=["date"])
    wide = _read_store_sales(raw)
    report: dict = {"store": STORE, "store_products": int(len(wide))}

    day_cols = [c for c in wide.columns if c.startswith("d_")]
    d2date = dict(zip(cal["d"], cal["date"]))
    keep_days = [d for d in day_cols if d2date[d] >= pd.Timestamp(START)]
    window = wide[keep_days]

    # selection: products sold on >= 80% of days in the window, then the
    # highest-volume PER_DEPT per department (balanced across the hierarchy)
    stats = pd.DataFrame({"item_id": wide["item_id"], "dept_id": wide["dept_id"], "cat_id": wide["cat_id"],
                          "selling_share": (window > 0).mean(axis=1), "volume": window.sum(axis=1)})
    eligible = stats[stats["selling_share"] >= 0.80]
    report["eligible_products"] = int(len(eligible))
    chosen = (eligible.sort_values("volume", ascending=False).groupby("dept_id").head(PER_DEPT))
    # fall back to most-selling items in thin departments
    for dept, g in stats.groupby("dept_id"):
        have = (chosen["dept_id"] == dept).sum()
        if have < PER_DEPT:
            extra = g[~g["item_id"].isin(chosen["item_id"])].sort_values("selling_share", ascending=False)
            chosen = pd.concat([chosen, extra.head(PER_DEPT - have)])
    items = set(chosen["item_id"])

    sub = wide[wide["item_id"].isin(items)]
    long = sub.melt(id_vars=["item_id", "dept_id", "cat_id"], value_vars=keep_days, var_name="d", value_name="units")
    long = long.merge(cal[["d", "date", "wm_yr_wk"]], on="d")

    prices = _read_store_prices(raw, items)
    long = long.merge(prices[["item_id", "wm_yr_wk", "sell_price"]], on=["item_id", "wm_yr_wk"], how="left")

    # --- cleaning ---------------------------------------------------------
    report["rows_before"] = int(len(long))
    report["missing_price_rows"] = int(long["sell_price"].isna().sum())
    long = long.sort_values(["item_id", "date"])
    long["sell_price"] = long.groupby("item_id")["sell_price"].transform(lambda s: s.ffill().bfill())
    report["negative_units"] = int((long["units"] < 0).sum())
    long["units"] = long["units"].clip(lower=0)
    # store-closed days (Christmas): every product 0 -> flag, keep as closed
    tot = long.groupby("date")["units"].sum()
    closed = set(tot[tot == 0].index)
    report["store_closed_days"] = sorted(str(d.date()) for d in closed)
    # outliers: cap at rolling-median + 6*MAD style bound (per item, 99.9 pct)
    caps = long.groupby("item_id")["units"].transform(lambda s: np.ceil(s.quantile(0.999)))
    report["capped_outliers"] = int((long["units"] > caps).sum())
    long["units"] = np.minimum(long["units"], caps).astype(int)
    long["closed"] = long["date"].isin(closed).astype(int)

    daily = long.rename(columns={"item_id": "sku", "sell_price": "price"})[
        ["sku", "date", "units", "price", "dept_id", "cat_id", "closed"]]
    catalog = (daily.groupby("sku").agg(dept_id=("dept_id", "first"), cat_id=("cat_id", "first"),
                                         unit_price=("price", "last")).reset_index())
    catalog["category"] = catalog["cat_id"].map(CATEGORY_LABEL)
    catalog["department"] = catalog["dept_id"].map(DEPT_LABEL)
    catalog["unit_price"] = catalog["unit_price"].round(2)

    calendar = cal[cal["date"] >= pd.Timestamp(START)][
        ["date", "event_name_1", "event_type_1", "event_name_2", "snap_CA"]].copy()
    calendar.to_csv(CALENDAR_FILE, index=False)
    report.update(products=int(daily["sku"].nunique()), days=int(daily["date"].nunique()),
                  date_min=str(daily["date"].min().date()), date_max=str(daily["date"].max().date()),
                  daily_rows=int(len(daily)), departments=sorted(daily["dept_id"].unique().tolist()))
    daily.to_csv(DAILY_FILE, index=False, compression="gzip")
    catalog.to_csv(CATALOG_FILE, index=False)
    CLEANING_REPORT.write_text(json.dumps(report, indent=2))
    return daily


def load_daily() -> pd.DataFrame:
    return pd.read_csv(DAILY_FILE, parse_dates=["date"], dtype={"sku": str})


def load_catalog() -> pd.DataFrame:
    return pd.read_csv(CATALOG_FILE, dtype={"sku": str})


def load_calendar() -> pd.DataFrame:
    return pd.read_csv(CALENDAR_FILE, parse_dates=["date"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    d = prepare()
    print(json.loads(CLEANING_REPORT.read_text()))
