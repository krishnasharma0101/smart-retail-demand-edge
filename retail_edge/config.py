"""Central configuration. Every value can be overridden with an env var."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return type(default)(raw)


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    raw_dir: Path = ROOT / "data_raw"
    model_dir: Path = ROOT / "models"
    web_dir: Path = ROOT / "web"
    store_id: str = field(default_factory=lambda: _env("STORE_ID", "store-001"))
    http_host: str = field(default_factory=lambda: _env("HTTP_HOST", "0.0.0.0"))
    http_port: int = field(default_factory=lambda: _env("HTTP_PORT", 8000))
    mqtt_host: str = field(default_factory=lambda: _env("MQTT_HOST", "127.0.0.1"))
    mqtt_port: int = field(default_factory=lambda: _env("MQTT_PORT", 1883))
    embedded_broker: bool = field(default_factory=lambda: _env("EMBEDDED_BROKER", "1") == "1")
    lead_time_days: int = field(default_factory=lambda: _env("LEAD_TIME_DAYS", 3))
    horizon_days: int = 7
    service_level_z: float = field(default_factory=lambda: _env("SERVICE_LEVEL_Z", 1.65))
    case_pack: int = field(default_factory=lambda: _env("CASE_PACK", 12))
    seed: int = 42


settings = Settings()
