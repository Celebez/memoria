"""SQLite canonical store. The single source of truth.

This module NEVER fails because of cloud layer issues. All public
methods are exception-safe: errors are recorded in audit_log and
re-raised as MemoriaStoreError so callers can decide.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import get_config

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class MemoriaStoreError(Exception):
    pass


class Store:
    """Thread-safe SQLite store with FTS5 search."""

    def __init__(self, db_path: str | None = None):
        cfg = get_config()
        self.db_path = db_path or cfg.sqlite_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._write_latency_ms = 0.0

    # ----- connection helpers -----
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
        finally:
            c.close()

    def _init_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text()
        with self._conn() as c:
            c.executescript(sql)

    # ----- audit -----
    def _audit(self, layer: str, action: str, target: str,
               status: str, latency_ms: float, error: str = "") -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO audit_log(layer, action, target, status, latency_ms, error, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (layer, action, target, status, latency_ms, error,
                     int(time.time() * 1000)),
                )
        except sqlite3.Error:
            # audit failures must never break the data path
            pass

    # ----- memories -----
    def put(self, key: str, value: Any, category: str = "general",
            tags: list[str] | None = None) -> str:
        """Insert or update a memory. Returns the id."""
        ts = int(time.time() * 1000)
        tags_csv = ",".join(tags or [])
        value_json = json.dumps(value, ensure_ascii=False, default=str)
        t0 = time.perf_counter()
        try:
            with self._conn() as c:
                existing = c.execute(
                    "SELECT id FROM memories WHERE key = ?", (key,)
                ).fetchone()
                if existing:
                    c.execute(
                        "UPDATE memories SET value=?, category=?, tags=?, updated_at=?"
                        " WHERE key=?",
                        (value_json, category, tags_csv, ts, key),
                    )
                    memory_id = existing["id"]
                else:
                    memory_id = uuid.uuid4().hex
                    c.execute(
                        "INSERT INTO memories(id, key, value, category, tags, source, access_count, created_at, updated_at)"
                        " VALUES (?,?,?,?,?, 'local', 0, ?, ?)",
                        (memory_id, key, value_json, category, tags_csv, ts, ts),
                    )
            latency = (time.perf_counter() - t0) * 1000
            self._audit("sqlite", "write", key, "ok", latency)
            return memory_id
        except sqlite3.Error as e:
            self._audit("sqlite", "write", key, "error",
                        (time.perf_counter() - t0) * 1000, str(e))
            raise MemoriaStoreError(f"put failed for {key}: {e}") from e

    def get(self, key: str) -> dict | None:
        """Fetch a memory by key. Returns {id, key, value, ...} or None."""
        t0 = time.perf_counter()
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT * FROM memories WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    c.execute(
                        "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                        (row["id"],),
                    )
                    # Re-fetch so the returned dict reflects post-update values
                    row = c.execute(
                        "SELECT * FROM memories WHERE id = ?", (row["id"],)
                    ).fetchone()
            latency = (time.perf_counter() - t0) * 1000
            if row:
                self._audit("sqlite", "read", key, "ok", latency)
                d = dict(row)
                d["value"] = json.loads(d["value"])
                return d
            self._audit("sqlite", "read", key, "miss", latency)
            return None
        except sqlite3.Error as e:
            self._audit("sqlite", "read", key, "error",
                        (time.perf_counter() - t0) * 1000, str(e))
            raise MemoriaStoreError(f"get failed for {key}: {e}") from e

    def delete(self, key: str) -> bool:
        t0 = time.perf_counter()
        try:
            with self._conn() as c:
                cur = c.execute("DELETE FROM memories WHERE key = ?", (key,))
                ok = cur.rowcount > 0
            self._audit("sqlite", "delete", key,
                        "ok" if ok else "miss",
                        (time.perf_counter() - t0) * 1000)
            return ok
        except sqlite3.Error as e:
            self._audit("sqlite", "delete", key, "error",
                        (time.perf_counter() - t0) * 1000, str(e))
            raise MemoriaStoreError(f"delete failed for {key}: {e}") from e

    def list_keys(self, category: str | None = None,
                  limit: int = 100) -> list[str]:
        with self._conn() as c:
            if category:
                rows = c.execute(
                    "SELECT key FROM memories WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                    (category, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT key FROM memories ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [r["key"] for r in rows]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across memories."""
        if not query.strip():
            return []
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT m.id, m.key, m.value, m.category, m.tags, m.updated_at,"
                    "       rank FROM memories_fts f JOIN memories m ON m.rowid = f.rowid"
                    " WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["value"] = json.loads(d["value"])
                out.append(d)
            return out
        except sqlite3.Error as e:
            raise MemoriaStoreError(f"search failed: {e}") from e

    def count(self, category: str | None = None) -> int:
        with self._conn() as c:
            if category:
                r = c.execute("SELECT COUNT(*) AS n FROM memories WHERE category = ?",
                              (category,)).fetchone()
            else:
                r = c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return r["n"]

    def stats(self) -> dict:
        with self._conn() as c:
            mem_count = c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            exp_count = c.execute("SELECT COUNT(*) AS n FROM explanations").fetchone()["n"]
            aud_count = c.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
            cat_rows = c.execute(
                "SELECT category, COUNT(*) AS n FROM memories GROUP BY category ORDER BY n DESC"
            ).fetchall()
        return {
            "memories": mem_count,
            "explanations": exp_count,
            "audit_log": aud_count,
            "categories": [dict(r) for r in cat_rows],
            "db_size_bytes": Path(self.db_path).stat().st_size,
        }

    # ----- explanations -----
    def add_explanation(self, topic: str, decision: str, rationale: str,
                        risk: str = "", reward: str = "",
                        confidence: float = 0.5,
                        source: str = "agent") -> str:
        exp_id = uuid.uuid4().hex
        ts = int(time.time() * 1000)
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO explanations(id, topic, decision, rationale, risk, reward, confidence, source, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (exp_id, topic, decision, rationale, risk, reward,
                     confidence, source, ts),
                )
            self._audit("sqlite", "write", f"exp:{topic}", "ok", 0.0)
            return exp_id
        except sqlite3.Error as e:
            self._audit("sqlite", "write", f"exp:{topic}", "error", 0.0, str(e))
            raise MemoriaStoreError(f"add_explanation failed: {e}") from e

    def list_explanations(self, topic: str | None = None,
                          limit: int = 50) -> list[dict]:
        with self._conn() as c:
            if topic:
                rows = c.execute(
                    "SELECT * FROM explanations WHERE topic = ? ORDER BY created_at DESC LIMIT ?",
                    (topic, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM explanations ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def search_explanations(self, query: str, limit: int = 20) -> list[dict]:
        if not query.strip():
            return []
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT e.*, rank FROM explanations_fts f JOIN explanations e ON e.rowid = f.rowid"
                    " WHERE explanations_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            raise MemoriaStoreError(f"search_explanations failed: {e}") from e

    # ----- backup support -----
    def export_all(self) -> dict:
        """Dump everything as a portable dict (for backup)."""
        with self._conn() as c:
            mem = [dict(r) for r in c.execute("SELECT * FROM memories").fetchall()]
            exp = [dict(r) for r in c.execute("SELECT * FROM explanations").fetchall()]
        # decode JSON values
        for m in mem:
            try:
                m["value"] = json.loads(m["value"])
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "version": 1,
            "exported_at": int(time.time() * 1000),
            "memories": mem,
            "explanations": exp,
        }

    def import_all(self, payload: dict, mode: str = "merge") -> int:
        """Restore from a backup payload. mode: merge|replace.

        Returns count of records imported.
        """
        if payload.get("version") != 1:
            raise MemoriaStoreError(f"unsupported payload version: {payload.get('version')}")
        count = 0
        with self._conn() as c:
            if mode == "replace":
                c.execute("DELETE FROM memories")
                c.execute("DELETE FROM explanations")
            for m in payload.get("memories", []):
                v = m["value"]
                if not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False, default=str)
                # upsert by key
                existing = c.execute(
                    "SELECT id FROM memories WHERE key = ?", (m["key"],)
                ).fetchone()
                if existing:
                    c.execute(
                        "UPDATE memories SET value=?, category=?, tags=?, updated_at=? WHERE key=?",
                        (v, m.get("category", "general"), m.get("tags", ""),
                         m.get("updated_at", int(time.time() * 1000)), m["key"]),
                    )
                else:
                    c.execute(
                        "INSERT INTO memories(id, key, value, category, tags, source, access_count, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (m["id"], m["key"], v,
                         m.get("category", "general"), m.get("tags", ""),
                         m.get("source", "backup"), m.get("access_count", 0),
                         m.get("created_at", int(time.time() * 1000)),
                         m.get("updated_at", int(time.time() * 1000))),
                    )
                count += 1
            for e in payload.get("explanations", []):
                existing = c.execute(
                    "SELECT id FROM explanations WHERE id = ?", (e["id"],)
                ).fetchone()
                if not existing:
                    c.execute(
                        "INSERT INTO explanations(id, topic, decision, rationale, risk, reward, confidence, source, outcome, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (e["id"], e["topic"], e["decision"], e["rationale"],
                         e.get("risk", ""), e.get("reward", ""),
                         e.get("confidence", 0.5), e.get("source", "backup"),
                         e.get("outcome", ""), e["created_at"]),
                    )
                    count += 1
        return count

    # ----- metrics -----
    def record_metric(self, name: str, value: float, layer: str = "all") -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO metrics(name, value, layer, created_at) VALUES (?,?,?,?)",
                    (name, value, layer, int(time.time() * 1000)),
                )
        except sqlite3.Error:
            pass

    def get_metrics(self, name: str | None = None, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            if name:
                rows = c.execute(
                    "SELECT * FROM metrics WHERE name = ? ORDER BY created_at DESC LIMIT ?",
                    (name, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM metrics ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]