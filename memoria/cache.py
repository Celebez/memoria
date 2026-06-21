"""Redis hot-cache layer (optional).

Implements write-through invalidation: when Store.put() succeeds, the
caller invokes Cache.put() to populate Redis. Cache misses fall back
to Store transparently.

This module is fully optional. If REDIS_ENABLED is False or no
connection can be established, all methods become silent no-ops so
the rest of the system keeps working.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .config import get_config

try:
    import redis as _redis
    REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    REDIS_AVAILABLE = False


class Cache:
    """Redis write-through cache. Safe no-op when unreachable."""

    def __init__(self):
        self.cfg = get_config()
        self._client = None
        self._connected = False
        self._connect()

    def _connect(self) -> None:
        if not REDIS_AVAILABLE:
            return
        if not (self.cfg.redis_enabled and self.cfg.redis_configured):
            return
        try:
            kwargs = {
                "host": self.cfg.redis_host,
                "port": self.cfg.redis_port,
                "db": self.cfg.redis_db,
                "decode_responses": True,
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
            }
            if self.cfg.redis_password:
                kwargs["password"] = self.cfg.redis_password
            if self.cfg.redis_url:
                self._client = _redis.Redis.from_url(
                    self.cfg.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
            else:
                self._client = _redis.Redis(**kwargs)
            self._client.ping()
            self._connected = True
        except Exception:
            self._client = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _key(self, key: str) -> str:
        return f"memoria:m:{key}"

    def get(self, key: str) -> Any | None:
        if not self._connected:
            return None
        try:
            raw = self._client.get(self._key(key))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            return None

    def put(self, key: str, value: Any, ttl: int | None = None) -> bool:
        if not self._connected:
            return False
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            self._client.set(self._key(key), payload,
                             ex=ttl or self.cfg.redis_ttl_hot)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> bool:
        if not self._connected:
            return False
        try:
            self._client.delete(self._key(key))
            return True
        except Exception:
            return False

    def invalidate_all(self) -> int:
        if not self._connected:
            return 0
        try:
            keys = list(self._client.scan_iter(match="memoria:m:*"))
            if keys:
                self._client.delete(*keys)
            return len(keys)
        except Exception:
            return 0

    def ping(self) -> bool:
        if not self._connected:
            return False
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def info(self) -> dict:
        if not self._connected:
            return {"connected": False, "error": "no client"}
        try:
            i = self._client.info("memory")
            return {
                "connected": True,
                "used_memory_human": i.get("used_memory_human", "?"),
                "used_memory_peak_human": i.get("used_memory_peak_human", "?"),
                "keyspace_keys": self._client.dbsize(),
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}

    # ----- read-through helper -----
    def get_or_load(self, key: str, loader) -> tuple[Any, str]:
        """Return (value, source) where source is 'redis' or 'sqlite'."""
        v = self.get(key)
        if v is not None:
            return v, "redis"
        v = loader()
        if v is not None:
            self.put(key, v)
            return v, "sqlite"
        return None, "miss"