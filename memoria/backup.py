"""Backup orchestrator.

Pushes a snapshot of the local store to all configured cloud layers.
Local backup (always on) creates a timestamped tar.gz under
data/backups/. Cloud layers are best-effort: failures are logged
but never block the local backup from succeeding.
"""

from __future__ import annotations

import gzip
import json
import tarfile
import time
from pathlib import Path

from .cloud import Cloud
from .config import get_config
from .store import Store


class Backup:
    def __init__(self, store: Store | None = None, cloud: Cloud | None = None):
        self.store = store or Store()
        self.cloud = cloud or Cloud()
        self.cfg = get_config()

    def snapshot_path(self, when: int | None = None) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(when or time.time()))
        return Path(self.cfg.backup_dir) / f"memoria-{ts}.tar.gz"

    def local(self) -> dict:
        """Create a tar.gz of the SQLite DB + explanations/ + memory_store.db."""
        out = self.snapshot_path()
        payload = self.store.export_all()
        manifest = {
            "created_at": int(time.time() * 1000),
            "version": 1,
            "counts": {
                "memories": len(payload["memories"]),
                "explanations": len(payload["explanations"]),
            },
            "schema": "memoria-v1",
        }
        with tarfile.open(out, "w:gz") as tar:
            # write manifest
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            _add_bytes(tar, "manifest.json", manifest_bytes)
            # write payload
            payload_bytes = json.dumps(payload, ensure_ascii=False, default=str).encode()
            _add_bytes(tar, "data.json", payload_bytes)
            # include the live sqlite file as belt-and-braces
            db_path = Path(self.cfg.sqlite_path)
            if db_path.exists():
                tar.add(str(db_path), arcname=f"live/{db_path.name}")
        self.store.record_metric("backup_local_ok", 1.0, "local")
        return {
            "path": str(out),
            "size_bytes": out.stat().st_size,
            "manifest": manifest,
        }

    def supabase(self) -> dict:
        """Push memories and explanations to Supabase tables."""
        if not (self.cfg.supabase_enabled and self.cfg.supabase_configured):
            return {"ok": False, "reason": "supabase not configured"}
        payload = self.store.export_all()
        mem_rows = []
        for m in payload["memories"]:
            v = m["value"]
            if not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False, default=str)
            mem_rows.append({
                "id": m["id"],
                "key": m["key"],
                "value": v,
                "category": m.get("category", "general"),
                "tags": m.get("tags", ""),
                "source": m.get("source", "local"),
                "access_count": m.get("access_count", 0),
                "created_at": m.get("created_at"),
                "updated_at": m.get("updated_at"),
            })
        exp_rows = []
        for e in payload["explanations"]:
            exp_rows.append({
                "id": e["id"],
                "topic": e["topic"],
                "decision": e["decision"],
                "rationale": e["rationale"],
                "risk": e.get("risk", ""),
                "reward": e.get("reward", ""),
                "confidence": e.get("confidence", 0.5),
                "source": e.get("source", "agent"),
                "outcome": e.get("outcome", ""),
                "created_at": e.get("created_at"),
            })

        results = {}
        # Batch upsert in chunks of 500 to stay under payload limits
        for tbl, rows in (("memories", mem_rows), ("explanations", exp_rows)):
            ok = True
            msg = ""
            for i in range(0, len(rows), 500):
                chunk = rows[i:i + 500]
                o, m = self.cloud.sb_upsert(tbl, chunk)
                if not o:
                    ok = False
                    msg = m
                    break
            results[tbl] = {"ok": ok, "rows": len(rows), "msg": msg}
            self.store.record_metric(f"backup_{tbl}_ok",
                                     1.0 if ok else 0.0, "supabase")
        return {"ok": all(r["ok"] for r in results.values()), "tables": results}

    def r2(self, include_local_archive: bool = True) -> dict:
        """Upload the local backup archive to Cloudflare R2."""
        if not (self.cfg.r2_enabled and self.cfg.r2_configured):
            return {"ok": False, "reason": "r2 not configured"}
        results = []
        if include_local_archive:
            local = self.local()
            data = Path(local["path"]).read_bytes()
            key = f"snapshots/{Path(local['path']).name}"
            ok, msg = self.cloud.r2_upload(key, data,
                                           content_type="application/gzip")
            results.append({"key": key, "ok": ok, "msg": msg})
        # Also push a compact JSON snapshot for quick restore
        payload = self.store.export_all()
        payload_bytes = json.dumps(payload, ensure_ascii=False,
                                   default=str).encode("utf-8")
        ts = time.strftime("%Y%m%d_%H%M%S")
        ok, msg = self.cloud.r2_upload(f"json/{ts}.json", payload_bytes,
                                       content_type="application/json")
        results.append({"key": f"json/{ts}.json", "ok": ok, "msg": msg})
        self.store.record_metric("backup_r2_ok",
                                 1.0 if all(r["ok"] for r in results) else 0.0,
                                 "r2")
        return {"ok": all(r["ok"] for r in results), "results": results}

    def all(self) -> dict:
        """Run every configured layer. Local is always attempted first."""
        out = {"local": None, "supabase": None, "r2": None}
        try:
            out["local"] = self.local()
        except Exception as e:
            out["local"] = {"ok": False, "error": str(e)}
        try:
            out["supabase"] = self.supabase()
        except Exception as e:
            out["supabase"] = {"ok": False, "error": str(e)}
        try:
            out["r2"] = self.r2()
        except Exception as e:
            out["r2"] = {"ok": False, "error": str(e)}
        return out


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    import io
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))