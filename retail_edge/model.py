"""Model registry + inference with per-tree prediction intervals."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

import joblib
import numpy as np

from .config import settings
from .features import FEATURES, to_matrix


class LatencyTracker:
    def __init__(self, n: int = 2000):
        self._v: deque[float] = deque(maxlen=n)
        self._lock = threading.Lock()
        self.count = 0

    def add(self, ms: float):
        with self._lock:
            self._v.append(ms)
            self.count += 1

    def summary(self) -> dict:
        with self._lock:
            v = np.array(self._v) if self._v else np.array([0.0])
        return {"count": self.count, "p50_ms": round(float(np.percentile(v, 50)), 2),
                "p95_ms": round(float(np.percentile(v, 95)), 2), "p99_ms": round(float(np.percentile(v, 99)), 2),
                "max_ms": round(float(v.max()), 2), "recent": [round(float(x), 2) for x in list(v)[-120:]]}


class DemandModel:
    def __init__(self, model_dir: Path | None = None, version: str | None = None):
        model_dir = model_dir or settings.model_dir
        latest = model_dir / "LATEST"
        if version is None:
            if not latest.exists():
                raise FileNotFoundError(f"no trained model in {model_dir}; run `python -m retail_edge.train`")
            version = latest.read_text().strip()
        vdir = model_dir / version
        self.meta = json.loads((vdir / "metadata.json").read_text())
        if self.meta["features"] != FEATURES:
            raise RuntimeError("model feature schema does not match code; retrain the model")
        self.model = joblib.load(vdir / "model.joblib")
        self.model.set_params(n_jobs=1)          # single core: predictable latency on edge CPUs
        self.version = version
        self.latency = LatencyTracker()
        self._lock = threading.Lock()
        self.sigma = self.meta.get("residual_std_daily", {})

    def predict_rows(self, rows: list[dict]) -> tuple[list[dict], float]:
        """Return point forecast + 10/90% interval from the spread of the trees."""
        X = to_matrix(rows)
        t0 = time.perf_counter()
        with self._lock:
            per_tree = np.stack([t.predict(X) for t in self.model.estimators_])
        ms = (time.perf_counter() - t0) * 1e3
        self.latency.add(ms)
        point = per_tree.mean(axis=0)
        lo, hi = np.percentile(per_tree, [10, 90], axis=0)
        out = [{"pred_7d": round(max(float(p), 0.0), 1), "p10": round(max(float(a), 0.0), 1),
                "p90": round(max(float(b), 0.0), 1)} for p, a, b in zip(point, lo, hi)]
        return out, ms

    def info(self, include_sample: bool = False) -> dict:
        m = dict(self.meta)
        if not include_sample:
            m.pop("test_sample", None)
        return m
