"""Environment-based configuration for Memoria.

All credentials come from environment variables (or .env file). The
system is designed to run with NO cloud credentials -- SQLite is the
canonical store and Redis/Supabase/R2 are optional accelerators and
backups.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# Project root: ~/memoria (parent of the memoria/ package)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
EXPLANATIONS_DIR = DATA_DIR / "explanations"

# Ensure runtime dirs exist at import time
for _p in (DATA_DIR, BACKUP_DIR, EXPLANATIONS_DIR):
    _p.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    """Read env var, falling back to .env file at project root."""
    val = os.environ.get(name)
    if val:
        return val
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
        except OSError:
            pass
    return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").lower().strip()
    return raw in ("1", "true", "yes", "on")


@dataclass
class Config:
    """Runtime configuration. Resolved once at startup."""

    # ---- Local (always-on) ----
    sqlite_path: str = str(DATA_DIR / "memoria.db")
    backup_dir: str = str(BACKUP_DIR)
    explanations_dir: str = str(EXPLANATIONS_DIR)

    # ---- Redis (optional hot cache) ----
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", ""))
    redis_host: str = field(default_factory=lambda: _env("REDIS_HOST", "127.0.0.1"))
    redis_port: int = field(default_factory=lambda: int(_env("REDIS_PORT", "6379")))
    redis_password: str = field(default_factory=lambda: _env("REDIS_PASSWORD", ""))
    redis_db: int = field(default_factory=lambda: int(_env("REDIS_DB", "0")))
    redis_ttl_hot: int = field(default_factory=lambda: int(_env("REDIS_TTL_HOT", "3600")))
    redis_enabled: bool = field(default_factory=lambda: _bool("REDIS_ENABLED", False))

    # ---- Supabase (optional durable backup) ----
    supabase_url: str = field(default_factory=lambda: _env("SUPABASE_URL", ""))
    supabase_key: str = field(default_factory=lambda: _env("SUPABASE_SERVICE_ROLE_KEY", ""))
    supabase_enabled: bool = field(default_factory=lambda: _bool("SUPABASE_ENABLED", False))

    # ---- Cloudflare R2 (optional cold archive) ----
    r2_account_id: str = field(default_factory=lambda: _env("R2_ACCOUNT_ID", ""))
    r2_access_key: str = field(default_factory=lambda: _env("R2_ACCESS_KEY_ID", ""))
    r2_secret_key: str = field(default_factory=lambda: _env("R2_SECRET_ACCESS_KEY", ""))
    r2_bucket: str = field(default_factory=lambda: _env("R2_BUCKET", "memoria-backups"))
    r2_endpoint: str = field(default_factory=lambda: _env("R2_ENDPOINT", ""))
    r2_enabled: bool = field(default_factory=lambda: _bool("R2_ENABLED", False))

    # ---- Behavior toggles ----
    write_through_cache: bool = field(default_factory=lambda: _bool("WRITE_THROUGH_CACHE", True))
    auto_backup_on_write: bool = field(default_factory=lambda: _bool("AUTO_BACKUP_ON_WRITE", False))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    # ---- Derived ----
    @property
    def redis_configured(self) -> bool:
        return bool(self.redis_url or (self.redis_host and self.redis_port))

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def r2_configured(self) -> bool:
        return bool(self.r2_account_id and self.r2_access_key and self.r2_secret_key)

    def layers_active(self) -> list[str]:
        """Return ordered list of layers currently usable."""
        layers = ["sqlite"]
        if self.redis_enabled and self.redis_configured:
            layers.append("redis")
        if self.supabase_enabled and self.supabase_configured:
            layers.append("supabase")
        if self.r2_enabled and self.r2_configured:
            layers.append("r2")
        return layers

    def summary(self) -> dict:
        return {
            "sqlite": self.sqlite_path,
            "redis_enabled": self.redis_enabled and self.redis_configured,
            "supabase_enabled": self.supabase_enabled and self.supabase_configured,
            "r2_enabled": self.r2_enabled and self.r2_configured,
            "layers_active": self.layers_active(),
            "write_through_cache": self.write_through_cache,
            "auto_backup_on_write": self.auto_backup_on_write,
        }


_singleton: Config | None = None


def get_config() -> Config:
    """Return cached Config singleton."""
    global _singleton
    if _singleton is None:
        _singleton = Config()
    return _singleton