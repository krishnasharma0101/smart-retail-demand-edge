"""Replenishment policy: (s, S)-style reorder point with model-driven demand.

Given the 7-day demand forecast D7 and its uncertainty:
    daily rate        d   = D7 / 7
    safety stock      SS  = z * sigma_d * sqrt(L)        (sigma_d: daily forecast error)
    reorder point     ROP = d * L + SS
    order-up-to       S   = D7 + d * L + SS             (cover review period + lead time)
    order quantity    Q   = ceil_to_case(S - on_hand - on_order)
Severity:
    CRITICAL  stock runs out before a new order could arrive (cover < L)
    HIGH      below reorder point
    MEDIUM    will not cover the next 7 days + safety stock
    OK        healthy
    OVERSTOCK more than 6 weeks of cover
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "OVERSTOCK": 3, "OK": 4}


@dataclass
class Recommendation:
    severity: str
    on_hand: float
    on_order: float
    daily_rate: float
    days_of_cover: float | None
    safety_stock: float
    reorder_point: float
    order_up_to: float
    order_qty: int
    reason: str

    def dict(self) -> dict:
        return asdict(self)


def recommend(pred_7d: float, on_hand: float, on_order: float = 0.0, sigma_daily: float = 0.0,
              lead_time_days: int = 3, z: float = 1.65, case_pack: int = 12, horizon: int = 7) -> Recommendation:
    if pred_7d < 0 or on_hand < 0 or on_order < 0:
        raise ValueError("quantities must be non-negative")
    if lead_time_days < 0 or case_pack < 1:
        raise ValueError("invalid lead time or case pack")
    d = pred_7d / horizon
    ss = z * max(sigma_daily, 0.0) * math.sqrt(max(lead_time_days, 1))
    rop = d * lead_time_days + ss
    s_level = pred_7d + d * lead_time_days + ss
    position = on_hand + on_order
    gap = s_level - position
    qty = int(math.ceil(gap / case_pack) * case_pack) if gap > 0 else 0
    cover = (on_hand / d) if d > 1e-9 else None

    if cover is not None and cover < lead_time_days and on_order == 0:
        sev, why = "CRITICAL", f"stock covers {cover:.1f} d, less than the {lead_time_days} d lead time"
    elif position < rop:
        sev, why = "HIGH", f"stock position {position:.0f} below reorder point {rop:.0f}"
    elif position < pred_7d + ss:
        sev, why = "MEDIUM", f"position {position:.0f} won't cover 7-day demand {pred_7d:.0f} + safety {ss:.0f}"
    elif cover is not None and cover > 42:
        sev, why, qty = "OVERSTOCK", f"{cover:.0f} days of cover", 0
    else:
        sev, why = "OK", "covered for horizon + lead time" if qty == 0 else "healthy"
    if sev == "OK":
        qty = 0
    return Recommendation(sev, round(on_hand, 1), round(on_order, 1), round(d, 2),
                          None if cover is None else round(cover, 1), round(ss, 1), round(rop, 1),
                          round(s_level, 1), qty, why)
