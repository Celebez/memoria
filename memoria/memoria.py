"""Memoria -- the unified facade.

Most callers only need this class:

    from memoria import Memoria
    m = Memoria()
    m.set("user:pref:theme", "dark", category="ui")
    v = m.get("user:pref:theme")
    m.explain("deploy strategy", "use rolling update",
              rationale="zero downtime", risk="rollback complexity",
              reward="no maintenance window", confidence=0.85)
    m.backup()                  # local always; cloud if configured
    m.restore("local:latest")   # restore from a layer
    print(m.stats())            # performance dashboard
"""

from __future__ import annotations

from typing import Any

from .backup import Backup
from .cache import Cache
from .cloud import Cloud
from .config import get_config
from .doctor import Doctor
from .explanations import Explanations
from .metrics import Metrics
from .restore import Restore
from .store import Store, MemoriaStoreError


class Memoria:
    """High-level API combining all layers."""

    def __init__(self):
        self.cfg = get_config()
        self.store = Store()
        self.cache = Cache()
        self.cloud = Cloud()
        self.explanations = Explanations(self.store)
        self.metrics = Metrics(self.store)
        self.backup = Backup(self.store, self.cloud)
        self.restore = Restore(self.store, self.cloud)
        self.doctor = Doctor()

    # ----- memories -----
    def set(self, key: str, value: Any, category: str = "general",
            tags: list[str] | None = None) -> str:
        memory_id = self.store.put(key, value, category, tags)
        if self.cfg.write_through_cache and self.cache.is_connected:
            self.cache.put(key, {"id": memory_id, "key": key, "value": value,
                                 "category": category})
        if self.cfg.auto_backup_on_write:
            try:
                self.backup.local()
            except Exception:
                pass
        return memory_id

    def get(self, key: str) -> dict | None:
        if self.cache.is_connected:
            cached = self.cache.get(key)
            if cached is not None:
                self.metrics.record_cache(True, 0.5)
                return cached
        result = self.store.get(key)
        if result and self.cache.is_connected:
            self.cache.put(key, result)
        self.metrics.record_cache(False, 1.0)
        return result

    def delete(self, key: str) -> bool:
        ok = self.store.delete(key)
        if self.cache.is_connected:
            self.cache.delete(key)
        return ok

    def list_keys(self, category: str | None = None,
                  limit: int = 100) -> list[str]:
        return self.store.list_keys(category=category, limit=limit)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        return self.store.search(query, limit=limit)

    # ----- explanations -----
    def explain(self, topic: str, decision: str, rationale: str,
                risk: str = "", reward: str = "",
                confidence: float = 0.5, source: str = "agent") -> str:
        return self.explanations.log(topic, decision, rationale,
                                     risk, reward, confidence, source)

    def list_explanations(self, topic: str | None = None,
                          limit: int = 50) -> list[dict]:
        return self.explanations.list(topic=topic, limit=limit)

    def search_explanations(self, query: str, limit: int = 20) -> list[dict]:
        return self.explanations.search(query, limit=limit)

    def record_outcome(self, exp_id: str, outcome: str) -> bool:
        return self.explanations.record_outcome(exp_id, outcome)

    # ----- convenience -----
    def stats(self) -> dict:
        return {
            "config": self.cfg.summary(),
            "store": self.store.stats(),
            "explanations": self.explanations.stats(),
            "performance": self.metrics.summary(),
        }

    def __repr__(self) -> str:
        layers = ", ".join(self.cfg.layers_active())
        return f"<Memoria layers=[{layers}] sqlite={self.cfg.sqlite_path}>"


# Re-export the error for callers that want to catch it
__all__ = ["Memoria", "MemoriaStoreError"]