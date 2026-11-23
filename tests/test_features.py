import datetime as dt

import numpy as np
import pytest

from retail_edge import data as D
from retail_edge.features import (FEATURES, EventCalendar, build_training_frame, default_calendar,
                                  features_from_history, synth_history)


@pytest.fixture(scope="module")
def daily():
    return D.load_daily()


def test_no_train_serve_skew(daily):
    """Vectorised training features == per-request edge features, row for row."""
    sku = "FOODS_3_090"
    g = daily[daily["sku"] == sku].reset_index(drop=True)
    frame = build_training_frame(daily[daily["sku"] == sku]).set_index("date")
    cal = default_calendar()
    for i in (40, 200, 600, len(g) - 9):
        d = g.loc[i, "date"]
        hist = g.loc[:i, "units"].tolist()
        ref = g.loc[i - 27:i, "price"].mean()
        f = features_from_history(hist, d.date(), g.loc[i, "price"], "FOODS", "FOODS_3", ref_price=ref, cal=cal)
        row = frame.loc[d]
        for k in FEATURES:
            assert f[k] == pytest.approx(row[k], rel=1e-9, abs=1e-9), k


def test_target_is_next_7_days(daily):
    g = daily[daily["sku"] == "HOBBIES_1_008"].reset_index(drop=True)
    frame = build_training_frame(daily[daily["sku"] == "HOBBIES_1_008"]).set_index("date")
    i = 100
    assert frame.loc[g.loc[i, "date"], "target"] == g.loc[i + 1:i + 7, "units"].sum()


def test_history_validation():
    with pytest.raises(ValueError):
        features_from_history([1] * 10, dt.date(2016, 1, 1), 1.0, "FOODS", "FOODS_1")
    with pytest.raises(ValueError):
        features_from_history([1] * 27 + [-1], dt.date(2016, 1, 1), 1.0, "FOODS", "FOODS_1")
    with pytest.raises(ValueError):
        features_from_history([1] * 28, dt.date(2016, 1, 1), 0.0, "FOODS", "FOODS_1")
    with pytest.raises(ValueError):
        features_from_history([1.0] * 27 + [float("nan")], dt.date(2016, 1, 1), 1.0, "FOODS", "FOODS_1")


def test_synth_history_preserves_totals():
    h = synth_history(10, 60, 250)
    assert len(h) == 28
    assert h[-1] == 10
    assert sum(h[-7:]) == pytest.approx(60)
    assert sum(h) == pytest.approx(250)
    with pytest.raises(ValueError):
        synth_history(70, 60, 250)


def test_calendar_projects_beyond_data():
    cal = default_calendar()
    assert cal.is_event(dt.date(2016, 12, 25))      # Christmas recurs
    assert EventCalendar.is_snap(dt.date(2030, 3, 5)) and not EventCalendar.is_snap(dt.date(2030, 3, 15))
    ev, snap = cal.horizon(dt.date(2015, 12, 20))
    assert ev >= 1 and 0 <= snap <= 7


def test_unknown_category_is_encoded_not_crashing():
    f = features_from_history(list(np.ones(28)), dt.date(2016, 1, 1), 2.0, "TOYS", "XYZ")
    assert f["category_code"] == -1 and f["dept_code"] == -1
