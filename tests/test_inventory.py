import pytest

from retail_edge.inventory import recommend


def test_critical_when_cover_below_lead_time():
    r = recommend(pred_7d=70, on_hand=15, lead_time_days=3)       # 10/day, 1.5 days of cover
    assert r.severity == "CRITICAL" and r.order_qty > 0


def test_ok_when_well_stocked():
    r = recommend(pred_7d=70, on_hand=200, sigma_daily=1, lead_time_days=3)
    assert r.severity == "OK" and r.order_qty == 0


def test_overstock():
    assert recommend(pred_7d=7, on_hand=500).severity == "OVERSTOCK"


def test_on_order_counts_toward_position():
    a = recommend(pred_7d=70, on_hand=25, sigma_daily=2)
    b = recommend(pred_7d=70, on_hand=25, on_order=120, sigma_daily=2)
    assert a.severity in ("HIGH", "CRITICAL") and b.severity == "OK"


def test_order_is_case_pack_multiple_and_reaches_target():
    r = recommend(pred_7d=100, on_hand=10, sigma_daily=3, lead_time_days=3, case_pack=12)
    assert r.order_qty % 12 == 0
    assert 10 + r.order_qty >= r.order_up_to


def test_zero_demand_has_no_cover_division():
    r = recommend(pred_7d=0, on_hand=0)
    assert r.days_of_cover is None and r.order_qty == 0


@pytest.mark.parametrize("kw", [{"pred_7d": -1, "on_hand": 1}, {"pred_7d": 1, "on_hand": -1},
                                {"pred_7d": 1, "on_hand": 1, "case_pack": 0}])
def test_rejects_invalid(kw):
    with pytest.raises(ValueError):
        recommend(**kw)
