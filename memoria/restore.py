"""Restore from any backup layer.

Use case: VPS disk dies, you spin up a fresh box, point Memoria at
the R2 archive, run `memoria restore r2 --latest`, and the local
store is repopulated. Same flow works for Supabase -> local.
"""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path

from .cloud import Cloud
from .config import get_config
from .store import Store


class Restore:
    def __init__(self, store: Store | None = None, cloud: Cloud | None = None):
        self.store = store or Store()
        self.cloud = cloud or Cloud()
        self.cfg = get_config()

    def local(self, archive_path: str, mode: str = "merge") -> dict:
        """Restore from a local tar.gz backup."""
        path = Path(archive_path)
        if not path.exists():
            return {"ok": False, "error": f"file not found: {archive_path}"}
        try:
            with tarfile.open(path, "r:gz") as tar:
                members = {m.name: m for m in tar.getmembers()}
                if "data.json" not in members:
                    return {"ok": False, "error": "missing data.json in archive"}
                fobj = tar.extractfile(members["data.json"])
                if fobj is None:
                    return {"ok": False, "error": "data.json unreadable"}
                payload = json.loads(fobj.read().decode("utf-8"))
            n = self.store.import_all(payload, mode=mode)
            return {"ok": True, "imported": n, "archive": str(path), "mode": mode}
        except (tarfile.TarError, json.JSONDecodeError, OSError) as e:
            return {"ok": False, "error": str(e)}

    def latest_local(self, mode: str = "merge") -> dict:
        """Restore from the most recent backup in data/backups/."""
        bd = Path(self.cfg.backup_dir)
        archives = sorted(bd.glob("memoria-*.tar.gz"), reverse=True)
        if not archives:
            return {"ok": False, "error": "no local backups found"}
        return self.local(str(archives[0]), mode=mode)

    def supabase(self, mode: str = "merge") -> dict:
        """Pull memories + explanations from Supabase into local store."""
        if not (self.cfg.supabase_enabled and self.cfg.supabase_configured):
            return {"ok": False, "error": "supabase not configured"}
        out: dict[str, dict] = {"memories": {"ok": False}, "explanations": {"ok": False}}
        ok_m, mem = self.cloud.sb_fetch_all("memories")
        if ok_m:
            payload = {"version": 1, "memories": mem, "explanations": []}
            n_mem = self.store.import_all(payload, mode=mode)
            out["memories"] = {"ok": True, "imported": n_mem}
        else:
            out["memories"] = {"ok": False, "error": mem}
        ok_e, exp = self.cloud.sb_fetch_all("explanations")
        if ok_e:
            payload = {"version": 1, "memories": [], "explanations": exp}
            n_exp = self.store.import_all(payload, mode=mode)
            out["explanations"] = {"ok": True, "imported": n_exp}
        else:
            out["explanations"] = {"ok": False, "error": exp}
        return {"ok": all(v.get("ok") for v in out.values()), "tables": out}

    def r2(self, mode: str = "merge", key: str | None = None) -> dict:
        """Pull the latest (or specified) JSON snapshot from R2."""
        if not (self.cfg.r2_enabled and self.cfg.r2_configured):
            return {"ok": False, "error": "r2 not configured"}
        if key is None:
            ok, keys = self.cloud.r2_list(prefix="json/")
            if not ok:
                return {"ok": False, "error": f"list failed: {keys}"}
            jsons = [k for k in keys if k.endswith(".json")]
            if not jsons:
                return {"ok": False, "error": "no json snapshots in R2"}
            key = sorted(jsons)[-1]
        ok, data = self.cloud.r2_download(key)
        if not ok:
            return {"ok": False, "error": data}
        try:
            payload = json.loads(data)
            n = self.store.import_all(payload, mode=mode)
            return {"ok": True, "imported": n, "key": key}
        except (json.JSONDecodeError, TypeError) as e:
            return {"ok": False, "error": str(e)}