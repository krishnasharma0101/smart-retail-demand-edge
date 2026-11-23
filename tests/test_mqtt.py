"""End-to-end over a real MQTT broker: simulated store -> edge -> MQTT out."""
import asyncio
import json
import socket
import threading
import time

import paho.mqtt.client as mqtt
import pytest

from retail_edge import broker as broker_mod
from retail_edge import topics as T
from retail_edge.config import settings


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def broker_port():
    port = settings.mqtt_port
    loop = asyncio.new_event_loop()
    started = threading.Event()

    async def run():
        b = broker_mod.Broker(broker_mod.config(port, _free_port()))
        await b.start(); started.set()
        await asyncio.Event().wait()

    th = threading.Thread(target=lambda: loop.run_until_complete(run()), daemon=True)
    th.start()
    assert started.wait(10)
    yield port


def test_store_to_edge_roundtrip(broker_port):
    from fastapi.testclient import TestClient
    from retail_edge.service import app

    got = {}
    sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-observer")
    sub.on_message = lambda c, u, m: got.setdefault(m.topic, json.loads(m.payload))
    sub.connect("127.0.0.1", broker_port); sub.subscribe(T.ROOT + "/edge/#", qos=1); sub.loop_start()

    with TestClient(app) as client:
        for _ in range(50):
            if client.get("/health").json()["mqtt_connected"]:
                break
            time.sleep(0.1)
        assert client.get("/health").json()["mqtt_connected"]
        pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-store")
        pub.connect("127.0.0.1", broker_port); pub.loop_start()
        sku = "FOODS_3_090"
        pub.publish(T.SHELF.format(sku=sku), json.dumps({"sku": sku, "on_hand": 3}), qos=1)
        pub.publish(T.POS.format(sku=sku), json.dumps({"sku": sku, "qty": 40, "price": 1.5}), qos=1)
        pub.publish(T.POS.format(sku=sku), b"not json", qos=1)                      # malformed
        pub.publish(T.POS.format(sku=sku), json.dumps({"sku": sku, "qty": -5}), qos=1)  # invalid
        pub.publish(T.CLOCK, json.dumps({"sim_date": "2015-11-22", "event": "day_close"}), qos=1)
        deadline = time.time() + 10
        while time.time() < deadline and T.REORDER.format(sku=sku) not in got:
            time.sleep(0.1)
        pub.loop_stop(); sub.loop_stop()

        assert T.STATUS in got and got[T.STATUS]["state"] == "online"
        assert got[T.FORECAST.format(sku=sku)]["pred_7d"] > 0
        assert got[T.ALERT.format(sku=sku)]["severity"] == "CRITICAL"            # 3 units on hand
        po = got[T.REORDER.format(sku=sku)]                                         # auto-reorder fired
        assert po["qty"] > 0 and po["qty"] % settings.case_pack == 0
        row = client.get(f"/stock/{sku}").json()
        assert row["history"][-1] == ["2015-11-22", 40]
        k = client.get("/metrics/edge").json()["kpis"]["counters"]
        assert k["rejected"] >= 2
