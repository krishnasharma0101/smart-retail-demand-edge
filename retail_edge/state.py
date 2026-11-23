"""In-memory store state owned by the edge node.

All mutation happens on one asyncio consumer task (MQTT callbacks only
enqueue), so there are no data races between the MQTT thread, HTTP handlers
and the inference loop.
"""
from __future__ import annotations

import datetime as dt
import itertools
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data as data_mod
from .config import settings
from .features import HORIZON, features_from_history
from .inventory import SEVERITY_ORDER, recommend
from .model import DemandModel

HIST_KEEP = 120


@dataclass
class Product:
    sku: str
    category: str
    department: str
    dept_label: str
    unit_price: float
    history: deque = field(default_factory=lambda: deque(maxlen=HIST_KEEP))   # (date, units)
    prices: deque = field(default_factory=lambda: deque(maxlen=28))
    today_units: int = 0
    on_hand: float | None = None
    on_order: float = 0.0
    open_pos: dict = field(default_factory=dict)
    forecast: dict | None = None
    rec: dict | None = None
    trail: deque = field(default_factory=lambda: deque(maxlen=HIST_KEEP))     # forecasts made each day
    pending: deque = field(default_factory=deque)                              # (origin_idx, pred, naive)
    lost_sales_est: float = 0.0


class StoreState:
    def __init__(self, model: DemandModel, seed_until: str):
        self.model = model
        self.auto_reorder = True
        self.lead_time = settings.lead_time_days
        self.z = settings.service_level_z
        self.sim_date: str | None = None
        self.day_index = 0
        self.products: dict[str, Product] = {}
        self.errors = deque(maxlen=2000)          # (abs err model, abs err naive, actual)
        self.alerts = deque(maxlen=300)
        self.po_seq = itertools.count(1)
        self.counters = defaultdict(int)
        self.msg_times: deque[float] = deque(maxlen=5000)
        self.started = time.time()
        self._seed(seed_until)

    def _seed(self, until: str):
        daily = data_mod.load_daily()
        cat = data_mod.load_catalog().set_index("sku")
        hist = daily[daily["date"] < pd.Timestamp(until)]
        for sku, g in hist.groupby("sku"):
            row = cat.loc[sku]
            p = Product(sku, row["cat_id"], row["dept_id"], row["department"], float(row["unit_price"]))
            g = g.tail(HIST_KEEP)
            p.history.extend((d.date().isoformat(), int(u)) for d, u in zip(g["date"], g["units"]))
            p.prices.extend(g["price"].tail(28).astype(float))
            p.unit_price = float(g["price"].iloc[-1])
            self.products[sku] = p
        self.sim_date = hist["date"].max().date().isoformat()
        self.run_forecasts(self.sim_date, publish=None)

    # ----------------------------------------------------------------- events
    def on_pos(self, sku: str, qty: int, price: float | None):
        p = self.products.get(sku)
        if p is None:
            self.counters["unknown_sku"] += 1
            return
        p.today_units += qty
        if price:
            p.unit_price = float(price)

    def on_shelf(self, sku: str, on_hand: float):
        p = self.products.get(sku)
        if p is not None:
            p.on_hand = float(on_hand)

    def on_delivery(self, sku: str, qty: int, po: str):
        p = self.products.get(sku)
        if p is None:
            return
        p.open_pos.pop(po, None)
        p.on_order = float(sum(p.open_pos.values()))

    def close_day(self, sim_date: str):
        self.sim_date = sim_date
        self.day_index += 1
        for p in self.products.values():
            p.history.append((sim_date, p.today_units))
            p.prices.append(p.unit_price)
            p.today_units = 0
            # settle forecasts whose 7-day window just completed
            while p.pending and self.day_index - p.pending[0][0] >= HORIZON:
                _, pred, naive = p.pending.popleft()
                actual = sum(u for _, u in list(p.history)[-HORIZON:])
                self.errors.append((abs(pred - actual), abs(naive - actual), actual))

    # ------------------------------------------------------------- inference
    def rows_for(self, p: Product, origin: str) -> dict:
        hist = [u for _, u in p.history]
        ref = float(np.mean(p.prices)) if p.prices else p.unit_price
        return features_from_history(hist, dt.date.fromisoformat(origin), p.unit_price, p.category,
                                     p.department, ref_price=ref)

    def run_forecasts(self, origin: str, publish) -> tuple[list[dict], float]:
        prods = list(self.products.values())
        rows = [self.rows_for(p, origin) for p in prods]
        preds, ms = self.model.predict_rows(rows)
        new_alerts = []
        for p, r, f in zip(prods, rows, preds):
            f.update(origin=origin, model_version=self.model.version)
            p.forecast = f
            p.trail.append((origin, f["pred_7d"], f["p10"], f["p90"]))
            p.pending.append((self.day_index, f["pred_7d"], r["roll_7_mean"] * HORIZON))
            if p.on_hand is not None:
                sigma = float(self.model.sigma.get(p.sku, f["pred_7d"] / HORIZON * 0.5))
                rec = recommend(f["pred_7d"], p.on_hand, p.on_order, sigma, self.lead_time, self.z,
                                settings.case_pack).dict()
                prev = (p.rec or {}).get("severity")
                p.rec = rec
                if rec["severity"] in ("CRITICAL", "HIGH") and prev != rec["severity"]:
                    a = {"ts": time.time(), "sim_date": origin, "sku": p.sku, "severity": rec["severity"],
                         "reason": rec["reason"], "order_qty": rec["order_qty"]}
                    self.alerts.appendleft(a)
                    new_alerts.append(a)
                if rec["days_of_cover"] is not None and rec["days_of_cover"] < 1 and p.on_hand == 0:
                    p.lost_sales_est += f["pred_7d"] / HORIZON
        return new_alerts, ms

    def refresh_rec(self, p: Product):
        """Re-evaluate one product's policy after stock / pipeline changed."""
        if p.forecast is None or p.on_hand is None:
            return
        sigma = float(self.model.sigma.get(p.sku, p.forecast["pred_7d"] / HORIZON * 0.5))
        p.rec = recommend(p.forecast["pred_7d"], p.on_hand, p.on_order, sigma, self.lead_time, self.z,
                          settings.case_pack).dict()

    def make_po(self, sku: str, qty: int) -> dict:
        p = self.products[sku]
        po = f"PO-{self.day_index:04d}-{next(self.po_seq):05d}"
        p.open_pos[po] = qty
        p.on_order = float(sum(p.open_pos.values()))
        self.refresh_rec(p)
        return {"po": po, "sku": sku, "qty": qty, "lead_time_days": self.lead_time, "sim_date": self.sim_date}

    # ------------------------------------------------------------- read models
    def product_row(self, p: Product) -> dict:
        hist = [u for _, u in p.history]
        return {"sku": p.sku, "category": p.category, "department": p.department, "dept_label": p.dept_label,
                "unit_price": round(p.unit_price, 2), "on_hand": p.on_hand, "on_order": p.on_order,
                "last_7": int(sum(hist[-7:])), "last_28": int(sum(hist[-28:])), "spark": hist[-28:],
                "forecast": p.forecast, "rec": p.rec}

    def rows(self) -> list[dict]:
        out = [self.product_row(p) for p in self.products.values()]
        out.sort(key=lambda r: (SEVERITY_ORDER.get((r["rec"] or {}).get("severity", "OK"), 9),
                                -(r["forecast"] or {}).get("pred_7d", 0)))
        return out

    def live_accuracy(self) -> dict:
        if not self.errors:
            return {"n": 0, "wape": None, "naive_wape": None, "mae": None}
        e = np.array(self.errors)
        tot = max(e[:, 2].sum(), 1e-9)
        return {"n": int(len(e)), "mae": round(float(e[:, 0].mean()), 2),
                "wape": round(float(e[:, 0].sum() / tot), 4), "naive_wape": round(float(e[:, 1].sum() / tot), 4)}

    def kpis(self) -> dict:
        rows = [p for p in self.products.values()]
        sev = defaultdict(int)
        for p in rows:
            sev[(p.rec or {}).get("severity", "UNKNOWN")] += 1
        demand = sum((p.forecast or {}).get("pred_7d", 0) for p in rows)
        revenue = sum((p.forecast or {}).get("pred_7d", 0) * p.unit_price for p in rows)
        at_risk_rev = sum((p.forecast or {}).get("pred_7d", 0) * p.unit_price for p in rows
                          if (p.rec or {}).get("severity") in ("CRITICAL", "HIGH"))
        now = time.time()
        rate = sum(1 for t in self.msg_times if now - t <= 10) / 10.0
        return {"sim_date": self.sim_date, "day_index": self.day_index, "skus": len(rows),
                "severity": dict(sev), "pred_units_7d": round(demand), "pred_revenue_7d": round(revenue, 2),
                "revenue_at_risk": round(at_risk_rev, 2), "on_order_units": round(sum(p.on_order for p in rows)),
                "open_pos": sum(len(p.open_pos) for p in rows), "auto_reorder": self.auto_reorder,
                "lead_time_days": self.lead_time, "msg_rate": rate, "accuracy": self.live_accuracy(),
                "latency": {k: v for k, v in self.model.latency.summary().items() if k != "recent"},
                "counters": dict(self.counters)}
