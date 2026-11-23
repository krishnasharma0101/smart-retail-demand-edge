import pytest
from fastapi.testclient import TestClient

from retail_edge.service import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:      # broker on test port is absent: API must still work
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_predict_known_sku(client):
    r = client.post("/predict", json={"sku": "FOODS_3_090", "current_stock": 50})
    assert r.status_code == 200
    b = r.json()
    assert b["pred_7d"] > 0 and b["p10"] <= b["p90"]
    assert b["recommendation"]["severity"] in {"CRITICAL", "HIGH", "MEDIUM", "OK", "OVERSTOCK"}
    assert b["latency_ms"] < 500


def test_predict_manual_summary(client):
    r = client.post("/predict", json={"category": "HOUSEHOLD", "department": "HOUSEHOLD_1", "unit_price": 3.97,
                                      "last_day": 9, "last_7_days": 52, "last_28_days": 190, "current_stock": 30})
    assert r.status_code == 200
    assert r.json()["source"] == "manual-summary"


def test_predict_manual_history(client):
    r = client.post("/predict", json={"category": "FOODS", "unit_price": 2.0, "history": [10] * 35, "date": "2016-03-01"})
    assert r.status_code == 200 and "recommendation" not in r.json()


@pytest.mark.parametrize("body", [
    {},                                                                    # nothing
    {"category": "FOODS", "unit_price": 2},                                # no sales
    {"category": "FOODS", "unit_price": 2, "last_day": 90, "last_7_days": 50, "last_28_days": 100},
    {"category": "FOODS", "unit_price": -1, "history": [1] * 28},
    {"category": "FOODS", "unit_price": 1, "history": [1] * 5},
    {"category": "FOODS", "unit_price": 1, "history": [1] * 27 + [-4]},
    {"category": "GROCERY", "unit_price": 1, "history": [1] * 28},
    {"category": "FOODS", "department": "HOBBIES_1", "unit_price": 1, "history": [1] * 28},
    {"sku": "DROP TABLE"},
    {"sku": "FOODS_3_090", "lead_time_days": 999},
    {"sku": "FOODS_3_090", "surprise": 1},
])
def test_predict_rejects_bad_input(client, body):
    r = client.post("/predict", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"] in ("validation_error", "invalid_input")


def test_unknown_sku_404(client):
    assert client.post("/predict", json={"sku": "FOODS_9_999"}).status_code == 404


def test_batch_partial_failure(client):
    r = client.post("/batch", json={"items": [{"sku": "FOODS_3_090"}, {"sku": "FOODS_9_999"}]})
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[0]["ok"] and not res[1]["ok"]


def test_batch_limits(client):
    assert client.post("/batch", json={"items": []}).status_code == 422


def test_stock_and_model_info(client):
    s = client.get("/stock").json()
    assert s["count"] == 49
    assert client.get("/stock/FOODS_3_090").json()["history"]
    assert client.get("/stock/NOPE").status_code == 404
    m = client.get("/model-info").json()
    assert "test_sample" not in m and m["algorithm"] == "RandomForestRegressor"


def test_reorder_without_broker_is_503(client):
    assert client.post("/stock/FOODS_3_090/reorder", json={"qty": 12}).status_code == 503


def test_dashboard_served(client):
    assert "Smart Retail Edge" in client.get("/").text
