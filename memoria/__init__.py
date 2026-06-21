"""Memoria - local-first multi-layer memory system.

A unified memory + explanation + performance tracking library backed by
SQLite (canonical), Redis (hot cache), Supabase (durable backup),
and Cloudflare R2 (cold archive). Designed to work offline; cloud
layers degrade gracefully when credentials are missing.
"""

from .config import Config, get_config
from .store import Store, MemoriaStoreError
from .cache import Cache
from .cloud import Cloud
from .explanations import Explanations
from .metrics import Metrics
from .backup import Backup
from .restore import Restore
from .doctor import Doctor
from .memoria import Memoria

__version__ = "1.0.0"
__all__ = [
    "Config",
    "get_config",
    "Store",
    "MemoriaStoreError",
    "Cache",
    "Cloud",
    "Explanations",
    "Metrics",
    "Backup",
    "Restore",
    "Doctor",
    "Memoria",
]