import json

import numpy as np
import pytest

from retail_edge.config import settings
from retail_edge.features import FEATURES
from retail_edge.model import DemandModel
from retail_edge.train import split, train
from retail_edge import data as D
from retail_edge.features import build_training_frame


@pytest.fixture(scope="module")
def model():
    return DemandModel()


def test_split_is_time_ordered_and_purged():
    frame = build_training_frame(D.load_daily())
    tr, te = split(frame)
    assert tr["date"].max() < te["date"].min()
    assert (te["date"].min() - tr["date"].max()).days >= 7      # no target leakage across the cut


def test_metadata_contract(model):
    m = model.meta
    assert m["features"] == FEATURES
    for k in ("random_forest", "decision_tree", "naive_last_7", "moving_avg_28"):
        assert {"mae", "rmse", "r2", "wape"} <= set(m["metrics_test"][k])


def test_random_forest_beats_baselines(model):
    t = model.meta["metrics_test"]
    assert t["random_forest"]["rmse"] < t["moving_avg_28"]["rmse"]
    assert t["random_forest"]["rmse"] < t["naive_last_7"]["rmse"]
    assert t["random_forest"]["r2"] > 0.8


def test_prediction_interval_ordering_and_determinism(model):
    rows = [{k: 1.0 for k in FEATURES} | {"roll_7_mean": v, "roll_14_mean": v, "roll_28_mean": v, "lag_1": v}
            for v in (0, 5, 50)]
    a, _ = model.predict_rows(rows)
    b, _ = model.predict_rows(rows)
    assert a == b
    for p in a:
        assert 0 <= p["p10"] <= p["pred_7d"] <= p["p90"] or p["p10"] <= p["p90"]
    assert a[0]["pred_7d"] < a[1]["pred_7d"] < a[2]["pred_7d"]      # more recent sales -> more demand


def test_quick_training_run_is_reproducible(tmp_path):
    v1 = train(quick=True, out_dir=tmp_path / "a")
    v2 = train(quick=True, out_dir=tmp_path / "b")
    m1 = json.loads((v1 / "metadata.json").read_text())["metrics_test"]
    m2 = json.loads((v2 / "metadata.json").read_text())["metrics_test"]
    assert m1 == m2
