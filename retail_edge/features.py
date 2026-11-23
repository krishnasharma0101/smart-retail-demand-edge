"""Feature engineering shared by training and edge inference.

`features_from_history` turns one product's recent daily sales into a
feature vector; `build_training_frame` computes the same definitions
vectorised over the whole dataset. tests/test_features.py asserts the two
paths agree, so train/serve skew is caught by CI rather than in the store.

Forecast origin = the last observed day t. Target = units sold over the next
H=7 days (t+1 .. t+7), which is the replenishment horizon.
"""
from __future__ import annotations

import datetime as dt
import math
from functools import lru_cache
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

HORIZON = 7
MIN_HISTORY = 28

CATEGORIES = ["FOODS", "HOBBIES", "HOUSEHOLD"]
DEPARTMENTS = ["FOODS_1", "FOODS_2", "FOODS_3", "HOBBIES_1", "HOBBIES_2", "HOUSEHOLD_1", "HOUSEHOLD_2"]

FEATURES = [
    # previous sales
    "lag_1", "lag_7", "lag_14", "lag_28",
    "roll_7_mean", "roll_14_mean", "roll_28_mean", "roll_28_std", "zero_share_28", "trend_7_28",
    # product
    "unit_price", "price_ratio_28", "category_code", "dept_code",
    # date / seasonality (of the first forecast day)
    "dow", "month", "doy_sin", "doy_cos", "is_weekend_next",
    # known-in-advance calendar effects inside the horizon
    "events_next_7", "snap_days_next_7",
]


def category_code(cat: str) -> int:
    cat = (cat or "").upper()
    return CATEGORIES.index(cat) if cat in CATEGORIES else -1


def dept_code(dept: str) -> int:
    dept = (dept or "").upper()
    return DEPARTMENTS.index(dept) if dept in DEPARTMENTS else -1


class EventCalendar:
    """Event and SNAP lookups. Dates beyond the data reuse the month/day
    pattern of the last 12 months of the calendar (fixed-date holidays repeat; SNAP in CA
    runs on days 1-10 of every month)."""

    def __init__(self, cal: Optional[pd.DataFrame] = None):
        self.events: set[dt.date] = set()
        self.md_events: set[tuple[int, int]] = set()
        if cal is not None and len(cal):
            ev = cal[cal["event_name_1"].notna()]["date"].dt.date
            self.events = set(ev)
            self.max_date = cal["date"].max().date()
            # the most recent 12 months define the projected yearly pattern
            recent = self.max_date - dt.timedelta(days=365)
            self.md_events = {(d.month, d.day) for d in self.events if d > recent}
        else:
            self.max_date = dt.date(1900, 1, 1)

    def is_event(self, d: dt.date) -> bool:
        if d <= self.max_date:
            return d in self.events
        return (d.month, d.day) in self.md_events

    @staticmethod
    def is_snap(d: dt.date) -> bool:
        return d.day <= 10

    def horizon(self, origin: dt.date) -> tuple[int, int]:
        days = [origin + dt.timedelta(days=i) for i in range(1, HORIZON + 1)]
        return sum(self.is_event(d) for d in days), sum(self.is_snap(d) for d in days)


@lru_cache(maxsize=1)
def default_calendar() -> EventCalendar:
    from .data import CALENDAR_FILE, load_calendar
    return EventCalendar(load_calendar() if CALENDAR_FILE.exists() else None)


def calendar_features(origin: dt.date, cal: EventCalendar) -> dict:
    nxt = origin + dt.timedelta(days=1)
    doy = nxt.timetuple().tm_yday
    ev, snap = cal.horizon(origin)
    return {
        "dow": nxt.weekday(),
        "month": nxt.month,
        "doy_sin": math.sin(2 * math.pi * doy / 365.25),
        "doy_cos": math.cos(2 * math.pi * doy / 365.25),
        "is_weekend_next": int(nxt.weekday() >= 5),
        "events_next_7": ev,
        "snap_days_next_7": snap,
    }


def features_from_history(history: Sequence[float], origin: dt.date, unit_price: float,
                          category: str, department: str, ref_price: Optional[float] = None,
                          cal: Optional[EventCalendar] = None) -> dict:
    """Features at forecast origin given daily units up to and including `origin`.

    history: oldest -> newest, >= 28 days. ref_price: average price over the
    last 28 days (defaults to unit_price, i.e. no promotion).
    """
    h = np.asarray(history, dtype=float)
    if h.size < MIN_HISTORY:
        raise ValueError(f"need at least {MIN_HISTORY} days of history, got {h.size}")
    if not np.all(np.isfinite(h)) or np.any(h < 0):
        raise ValueError("history must be finite and non-negative")
    if not (unit_price > 0):
        raise ValueError("unit_price must be > 0")
    cal = cal or default_calendar()
    w7, w14, w28 = h[-7:], h[-14:], h[-28:]
    r28 = float(w28.mean())
    f = {
        "lag_1": h[-1], "lag_7": h[-7], "lag_14": h[-14], "lag_28": h[-28],
        "roll_7_mean": float(w7.mean()), "roll_14_mean": float(w14.mean()), "roll_28_mean": r28,
        "roll_28_std": float(w28.std(ddof=1)),
        "zero_share_28": float((w28 == 0).mean()),
        "trend_7_28": (float(w7.mean()) + 1.0) / (r28 + 1.0),
        "unit_price": float(unit_price),
        "price_ratio_28": float(unit_price) / float(ref_price or unit_price),
        "category_code": category_code(category),
        "dept_code": dept_code(department),
    }
    f.update(calendar_features(origin, cal))
    return f


def synth_history(last_day: float, last_7: float, last_28: float) -> list[float]:
    """Plausible 28-day series from three summary figures (manual entry path).

    Days 1-21 share (last_28-last_7), days 22-27 share (last_7-last_day) and
    day 28 is last_day.
    """
    if not (0 <= last_day <= last_7 <= last_28):
        raise ValueError("expected 0 <= last_day <= last_7_days <= last_28_days")
    early = (last_28 - last_7) / 21.0
    mid = (last_7 - last_day) / 6.0
    return [early] * 21 + [mid] * 6 + [float(last_day)]


def build_training_frame(daily: pd.DataFrame, cal: Optional[EventCalendar] = None) -> pd.DataFrame:
    """Vectorised equivalent of features_from_history for every (sku, date)."""
    cal = cal or default_calendar()
    dates = pd.DatetimeIndex(sorted(daily["date"].unique()))
    calf = pd.DataFrame([calendar_features(d.date(), cal) for d in dates])
    calf["date"] = dates
    out = []
    for sku, g in daily.sort_values("date").groupby("sku", sort=True):
        g = g.reset_index(drop=True)
        u = g["units"].astype(float)
        p = g["price"].astype(float)
        f = pd.DataFrame({"sku": sku, "date": g["date"]})
        f["lag_1"], f["lag_7"], f["lag_14"], f["lag_28"] = u, u.shift(6), u.shift(13), u.shift(27)
        f["roll_7_mean"] = u.rolling(7).mean()
        f["roll_14_mean"] = u.rolling(14).mean()
        f["roll_28_mean"] = u.rolling(28).mean()
        f["roll_28_std"] = u.rolling(28).std(ddof=1)
        f["zero_share_28"] = (u == 0).astype(float).rolling(28).mean()
        f["trend_7_28"] = (f["roll_7_mean"] + 1.0) / (f["roll_28_mean"] + 1.0)
        f["unit_price"] = p
        f["price_ratio_28"] = p / p.rolling(28).mean()
        f["category_code"] = g["cat_id"].map(category_code)
        f["dept_code"] = g["dept_id"].map(dept_code)
        f["target"] = u[::-1].rolling(HORIZON).sum()[::-1].shift(-1)
        out.append(f)
    frame = pd.concat(out, ignore_index=True).merge(calf, on="date", how="left")
    return frame.dropna(subset=FEATURES).reset_index(drop=True)


def to_matrix(rows: Iterable[dict]) -> np.ndarray:
    return np.array([[float(r[c]) for c in FEATURES] for r in rows], dtype=float)
