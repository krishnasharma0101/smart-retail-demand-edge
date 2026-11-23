"""Store simulator: POS terminals + shelf sensors + receiving dock over MQTT.

Replays the held-out test period of the real dataset (data the model never
saw), one simulated trading day every SIM_DAY_SECONDS. For every product it
publishes POS sale lines through the day, a shelf-sensor reading after each
sale, and a day-close clock tick. It listens for purchase orders from the
edge service and delivers them after LEAD_TIME_DAYS.

Sales are capped by what is physically on the shelf, so a stock-out shows up
as lost sales exactly as it would in a real store.

    python -m retail_edge.simulator [--speed 4] [--start 2015-11-22] [--days 120]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import signal
import threading
import time
from collections import defaultdict

import numpy as np
import pandas as pd
import paho.mqtt.client as mqtt

from . import data as data_mod
from . import topics as T
from .config import settings

log = logging.getLogger("simulator")


class StoreSimulator:
    def __init__(self, start: str, days: int, speed: float, seed: int = 42, intraday: int = 4):
        self.rng = random.Random(seed)
        daily = data_mod.load_daily()
        cat = data_mod.load_catalog().set_index("sku")
        start_ts = pd.Timestamp(start)
        self.days = sorted(pd.Timestamp(d) for d in daily["date"].unique() if pd.Timestamp(d) >= start_ts)[:days]
        self.demand = {(r.sku, r.date): int(r.units) for r in daily[daily["date"].isin(self.days)].itertuples()}
        self.price = {(r.sku, r.date): float(r.price) for r in daily[daily["date"].isin(self.days)].itertuples()}
        hist = daily[daily["date"] < start_ts].groupby("sku")["units"].apply(lambda s: s.tail(28).mean())
        self.skus = list(cat.index)
        # opening stock: 2..12 days of recent average demand (some start at risk)
        self.on_hand = {s: int(max(6, hist.get(s, 5) * self.rng.uniform(2, 12))) for s in self.skus}
        self.pipeline: dict[int, list[tuple[str, int, str]]] = defaultdict(list)   # day_idx -> deliveries
        self.speed, self.intraday = speed, intraday
        self.day_idx = 0
        self.lost = defaultdict(int)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{settings.store_id}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ---- MQTT ------------------------------------------------------------
    def _on_connect(self, c, userdata, flags, rc, props=None):
        log.info("connected to broker rc=%s", rc)
        c.subscribe(T.REORDER.format(sku="+"), qos=1)

    def _on_message(self, c, userdata, msg):
        try:
            body = json.loads(msg.payload)
            sku = msg.topic.rsplit("/", 1)[-1]
            qty = int(body["qty"])
            if sku not in self.on_hand or not (0 < qty <= 100_000):
                raise ValueError("bad order")
        except Exception as e:  # never let a malformed command kill the store
            log.warning("rejected command on %s: %s", msg.topic, e)
            return
        lead = int(body.get("lead_time_days", settings.lead_time_days))
        with self._lock:
            self.pipeline[self.day_idx + lead].append((sku, qty, body.get("po", "?")))
        log.info("PO %s accepted: %s x%d, ETA day+%d", body.get("po"), sku, qty, lead)

    def pub(self, topic: str, payload: dict, retain: bool = False, qos: int = 0):
        self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=qos, retain=retain)

    # ---- simulation --------------------------------------------------------
    def step_day(self):
        day = self.days[self.day_idx]
        ddate = day.date().isoformat()
        with self._lock:
            arrivals = self.pipeline.pop(self.day_idx, [])
        for sku, qty, po in arrivals:          # morning deliveries
            self.on_hand[sku] += qty
            self.pub(T.DELIVERY.format(sku=sku), {"sim_date": ddate, "sku": sku, "qty": qty, "po": po}, qos=1)
            self.pub(T.SHELF.format(sku=sku), {"sim_date": ddate, "sku": sku, "on_hand": self.on_hand[sku]})
        self.pub(T.CLOCK, {"sim_date": ddate, "event": "day_open", "day_index": self.day_idx})
        # split each product's real demand into intraday POS batches
        slices = []
        for sku in self.skus:
            q = self.demand.get((sku, day), 0)
            cuts = sorted(self.rng.randint(0, q) for _ in range(self.intraday - 1)) if q else []
            parts = [b - a for a, b in zip([0] + cuts, cuts + [q])] if q else []
            slices.append((sku, parts))
        tick = self.speed / (self.intraday + 1)
        for k in range(self.intraday):
            if self._stop.is_set():
                return
            for sku, parts in slices:
                if k >= len(parts) or parts[k] == 0:
                    continue
                want = parts[k]
                sold = min(want, self.on_hand[sku])
                self.lost[sku] += want - sold
                if sold:
                    self.on_hand[sku] -= sold
                    self.pub(T.POS.format(sku=sku), {"sim_date": ddate, "sku": sku, "qty": sold,
                                                     "price": self.price.get((sku, day)), "slot": k})
                self.pub(T.SHELF.format(sku=sku), {"sim_date": ddate, "sku": sku, "on_hand": self.on_hand[sku]})
            time.sleep(tick)
        self.pub(T.CLOCK, {"sim_date": ddate, "event": "day_close", "day_index": self.day_idx}, qos=1)
        self.pub(T.SIM_STATUS, {"sim_date": ddate, "day_index": self.day_idx, "days_total": len(self.days),
                                "lost_units": sum(self.lost.values()), "speed_s_per_day": self.speed}, retain=True)
        time.sleep(tick)
        self.day_idx += 1

    def run(self):
        self.client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
        self.client.loop_start()
        time.sleep(1.0)
        # publish opening shelf state so the edge knows stock before day 1
        start = self.days[0].date().isoformat()
        for sku in self.skus:
            self.pub(T.SHELF.format(sku=sku), {"sim_date": start, "sku": sku, "on_hand": self.on_hand[sku]})
        try:
            while self.day_idx < len(self.days) and not self._stop.is_set():
                self.step_day()
        finally:
            self.client.loop_stop()
            self.client.disconnect()

    def stop(self, *_):
        self._stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=float(__import__("os").getenv("SIM_DAY_SECONDS", "4")),
                    help="wall-clock seconds per simulated day")
    ap.add_argument("--start", default="2015-11-22")
    ap.add_argument("--days", type=int, default=170)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s sim %(levelname)s %(message)s")
    sim = StoreSimulator(a.start, a.days, a.speed, seed=settings.seed)
    signal.signal(signal.SIGTERM, sim.stop)
    signal.signal(signal.SIGINT, sim.stop)
    sim.run()


if __name__ == "__main__":
    main()
