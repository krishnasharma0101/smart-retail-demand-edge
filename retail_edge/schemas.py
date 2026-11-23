"""Request/response validation for the HTTP API."""
from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .features import CATEGORIES, DEPARTMENTS, MIN_HISTORY

SKU_PATTERN = r"^[A-Z]+_[0-9]_[0-9]{3}$"


class PredictRequest(BaseModel):
    """Either a known `sku` (history comes from the edge's live store state)
    or a manual product description with recent sales."""
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"sku": "FOODS_3_090"},
        {"category": "FOODS", "department": "FOODS_3", "unit_price": 1.98, "date": "2016-05-01",
         "last_day": 40, "last_7_days": 260, "last_28_days": 980, "current_stock": 120},
    ]})

    sku: Optional[str] = Field(None, pattern=SKU_PATTERN, max_length=20)
    date: Optional[dt.date] = Field(None, description="forecast origin (last observed day)")
    category: Optional[Literal["FOODS", "HOBBIES", "HOUSEHOLD"]] = None
    department: Optional[str] = Field(None, max_length=20)
    unit_price: Optional[float] = Field(None, gt=0, le=10_000)
    ref_price: Optional[float] = Field(None, gt=0, le=10_000, description="avg price over last 28 days")
    history: Optional[list[float]] = Field(None, min_length=MIN_HISTORY, max_length=400)
    last_day: Optional[float] = Field(None, ge=0, le=1_000_000)
    last_7_days: Optional[float] = Field(None, ge=0, le=1_000_000)
    last_28_days: Optional[float] = Field(None, ge=0, le=1_000_000)
    current_stock: Optional[float] = Field(None, ge=0, le=10_000_000)
    on_order: float = Field(0, ge=0, le=10_000_000)
    lead_time_days: Optional[int] = Field(None, ge=0, le=60)

    @model_validator(mode="after")
    def _check(self):
        if self.history is not None and any((v < 0 or v != v or v > 1_000_000) for v in self.history):
            raise ValueError("history values must be between 0 and 1,000,000")
        if self.department is not None and self.department not in DEPARTMENTS:
            raise ValueError(f"department must be one of {DEPARTMENTS}")
        if self.department and self.category and not self.department.startswith(self.category):
            raise ValueError("department does not belong to category")
        if self.sku is None:
            if self.category is None or self.unit_price is None:
                raise ValueError("manual prediction needs category and unit_price (or give a known sku)")
            summaries = [self.last_day, self.last_7_days, self.last_28_days]
            if self.history is None and any(v is None for v in summaries):
                raise ValueError("give either history (>=28 daily values) or last_day, last_7_days and last_28_days")
            if self.history is None and not (self.last_day <= self.last_7_days <= self.last_28_days):
                raise ValueError("expected last_day <= last_7_days <= last_28_days")
        return self


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[PredictRequest] = Field(..., min_length=1, max_length=500)


class ReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qty: Optional[int] = Field(None, gt=0, le=100_000)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_reorder: Optional[bool] = None
    lead_time_days: Optional[int] = Field(None, ge=0, le=60)
    service_level_z: Optional[float] = Field(None, ge=0, le=4)
