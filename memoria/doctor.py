"""Doctor -- health check for every layer.

Returns a JSON-friendly report showing which layers are reachable,
how full each is, and any errors detected. Used by cron and by the
'bin/memoria doctor' CLI command.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .cache import Cache
from .cloud import Cloud
from .config import get_config
from .store import Store


class Doctor:
    def __init__(self):
        self.cfg = get_config()
        self.store = Store()
        self.cache = Cache()
        self.cloud = Cloud()

    def run(self) -> dict:
        report = {
            "config": self.cfg.summary(),
            "sqlite": self._check_sqlite(),
            "redis": self._check_redis(),
            "supabase": self._check_supabase(),
            "r2": self._check_r2(),
            "summary": {},
        }
        report["summary"] = {
            "layers_active": self.cfg.layers_active(),
            "healthy": all(
                report[k]["status"] in ("ok", "disabled", "skipped")
                for k in ("sqlite", "redis", "supabase", "r2")
            ),
        }
        return report

    def _check_sqlite(self) -> dict:
        try:
            stats = self.store.stats()
            size = Path(self.cfg.sqlite_path).stat().st_size
            return {
                "status": "ok",
                "path": self.cfg.sqlite_path,
                "size_bytes": size,
                "memories": stats["memories"],
                "explanations": stats["explanations"],
            }
        except sqlite3.Error as e:
            return {"status": "error", "error": str(e)}

    def _check_redis(self) -> dict:
        if not self.cfg.redis_enabled:
            return {"status": "disabled"}
        if not self.cfg.redis_configured:
            return {"status": "skipped", "reason": "no credentials"}
        info = self.cache.info()
        if info.get("connected"):
            return {"status": "ok", **info}
        return {"status": "error", "error": info.get("error", "not connected")}

    def _check_supabase(self) -> dict:
        if not self.cfg.supabase_enabled:
            return {"status": "disabled"}
        if not self.cfg.supabase_configured:
            return {"status": "skipped", "reason": "no credentials"}
        s = self.cloud.status()["supabase"]
        return {"status": "ok" if s["tables_ok"] else "error", **s}

    def _check_r2(self) -> dict:
        if not self.cfg.r2_enabled:
            return {"status": "disabled"}
        if not self.cfg.r2_configured:
            return {"status": "skipped", "reason": "no credentials"}
        s = self.cloud.status()["r2"]
        return {"status": "ok" if s["configured"] else "error", **s}