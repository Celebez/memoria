"""Performance tracking.

Records latency and hit-rate metrics across all layers. Provides a
small in-process dashboard via Memoria.stats().
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from contextlib import contextmanager
from typing import Any

from .store import Store


class Metrics:
    """Latency tracker + hit-rate counter. Backed by SQLite for persistence."""

    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self._hit_window: deque[bool] = deque(maxlen=1000)
        self._latency_window_ms: deque[float] = deque(maxlen=1000)

    def record_cache(self, hit: bool, latency_ms: float) -> None:
        self._hit_window.append(hit)
        self._latency_window_ms.append(latency_ms)
        self.store.record_metric("cache_hit", 1.0 if hit else 0.0, "redis")
        self.store.record_metric("cache_latency_ms", latency_ms, "redis")

    def record_layer(self, layer: str, action: str, latency_ms: float,
                     success: bool) -> None:
        self.store.record_metric(f"{layer}_{action}_ms", latency_ms, layer)
        self.store.record_metric(f"{layer}_{action}_ok",
                                 1.0 if success else 0.0, layer)

    @contextmanager
    def timer(self, layer: str, action: str):
        t0 = time.perf_counter()
        success = True
        try:
            yield
        except Exception:
            success = False
            raise
        finally:
            self.record_layer(layer, action,
                              (time.perf_counter() - t0) * 1000, success)

    def summary(self) -> dict:
        if not self._hit_window:
            cache = {"hit_rate": 0.0, "samples": 0}
        else:
            cache = {
                "hit_rate": round(sum(self._hit_window) / len(self._hit_window), 3),
                "samples": len(self._hit_window),
            }
        if not self._latency_window_ms:
            lat = {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "samples": 0}
        else:
            sorted_lat = sorted(self._latency_window_ms)
            n = len(sorted_lat)
            lat = {
                "p50_ms": round(sorted_lat[max(0, int(n * 0.50) - 1)], 3),
                "p95_ms": round(sorted_lat[max(0, int(n * 0.95) - 1)], 3),
                "p99_ms": round(sorted_lat[max(0, int(n * 0.99) - 1)], 3),
                "samples": n,
            }
        # Recent metrics from SQLite
        recent = self.store.get_metrics(limit=200)
        layer_breakdown: dict[str, dict[str, float]] = {}
        for m in recent:
            layer_breakdown.setdefault(m["layer"], {}).setdefault(m["name"], []).append(m["value"])
        for layer, names in layer_breakdown.items():
            for k in list(names.keys()):
                vals = names[k]
                names[k] = {
                    "count": len(vals),
                    "avg": round(statistics.mean(vals), 3) if vals else 0,
                }
        return {"cache": cache, "latency": lat, "by_layer": layer_breakdown}