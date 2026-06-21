"""Memoria unit tests.

Run with: cd ~/memoria && python3 -m pytest tests/ -v
Or without pytest: python3 tests/test_memoria.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# allow `python3 tests/test_memoria.py` from project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memoria import Memoria, MemoriaStoreError  # noqa: E402
from memoria.config import get_config  # noqa: E402
from memoria.store import Store  # noqa: E402
from memoria.cache import Cache  # noqa: E402
from memoria.backup import Backup  # noqa: E402
from memoria.restore import Restore  # noqa: E402
from memoria.explanations import Explanations  # noqa: E402
from memoria.metrics import Metrics  # noqa: E402
from memoria.doctor import Doctor  # noqa: E402


def _make_tmp_copy() -> Path:
    """Create a temp project dir with isolated sqlite path.

    Returns the tmp Path. The caller is responsible for shutil.rmtree().
    The active Config singleton is rewritten to point at the temp paths.
    """
    tmp = Path(tempfile.mkdtemp(prefix="memoria_test_"))
    (tmp / "data" / "backups").mkdir(parents=True)
    (tmp / "data" / "explanations").mkdir(parents=True)
    # Symlink the schema + memoria package so we reuse the code
    (tmp / "schema.sql").symlink_to(ROOT / "schema.sql")
    (tmp / "memoria").symlink_to(ROOT / "memoria")
    # Override config to point at the temp
    import memoria.config as cfg_mod
    cfg_mod._singleton = None
    c = get_config()
    c.sqlite_path = str(tmp / "data" / "memoria.db")
    c.backup_dir = str(tmp / "data" / "backups")
    c.explanations_dir = str(tmp / "data" / "explanations")
    c.redis_enabled = False
    c.supabase_enabled = False
    c.r2_enabled = False
    return tmp


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def test_store_roundtrip() -> None:
    _section("store roundtrip")
    tmp = _make_tmp_copy()
    s = Store()
    mid = s.put("hello", {"msg": "world"}, category="greeting", tags=["a", "b"])
    _ok(f"put returned id={mid[:8]}")
    row = s.get("hello")
    assert row is not None and row["value"] == {"msg": "world"}, "value mismatch"
    _ok("get returns correct value")
    assert row["access_count"] >= 1, "access_count not incremented"
    _ok(f"access_count incremented to {row['access_count']}")
    assert s.count() == 1
    _ok("count == 1")
    shutil.rmtree(tmp, ignore_errors=True)


def test_store_search() -> None:
    _section("store FTS5 search")
    tmp = _make_tmp_copy()
    s = Store()
    s.put("user:pref:theme", {"value": "dark"}, category="ui")
    s.put("user:pref:lang", {"value": "id"}, category="ui")
    s.put("agent:strategy", {"value": "rolling update"}, category="agent")
    rows = s.search("theme")
    assert len(rows) >= 1, "FTS5 missed 'theme'"
    _ok(f"FTS5 found {len(rows)} rows for 'theme'")
    rows = s.search("rolling update")
    assert any("strategy" in r["key"] for r in rows)
    _ok("FTS5 found 'rolling update'")
    shutil.rmtree(tmp, ignore_errors=True)


def test_explanations() -> None:
    _section("explanations")
    tmp = _make_tmp_copy()
    s = Store()
    e = Explanations(s)
    eid = e.log(
        topic="deploy strategy", decision="rolling update",
        rationale="zero downtime for users", risk="rollback complexity",
        reward="no maintenance window required", confidence=0.85,
    )
    _ok(f"log returned id={eid[:8]}")
    items = e.list(topic="deploy strategy")
    assert len(items) == 1
    _ok("list(topic=...) returns 1 row")
    # Markdown mirror should exist
    md_files = list(Path(get_config().explanations_dir).glob("*.md"))
    assert md_files, "no markdown mirror file"
    _ok(f"markdown mirror at {md_files[0].name}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_backup_local_and_restore() -> None:
    _section("backup -> restore roundtrip")
    tmp = _make_tmp_copy()
    s1 = Store()
    s1.put("alpha", {"v": 1})
    s1.put("beta", {"v": 2})
    s1.add_explanation("topic-a", "decision-a", "rationale-a",
                       risk="risk-a", reward="reward-a", confidence=0.7)
    b = Backup(s1)
    out = b.local()
    assert Path(out["path"]).exists(), "backup file not created"
    _ok(f"local backup created at {out['path']} ({out['size_bytes']} bytes)")
    # Wipe via direct SQL so we share the schema but lose data
    with s1._conn() as c:
        c.execute("DELETE FROM memories")
        c.execute("DELETE FROM explanations")
    assert s1.count() == 0
    _ok("wiped local store to simulate cold start")
    r = Restore(s1)
    res = r.local(out["path"], mode="replace")
    assert res["ok"], f"restore failed: {res}"
    _ok(f"restored {res['imported']} records")
    assert s1.count() == 2
    _ok("store now has 2 memories")
    shutil.rmtree(tmp, ignore_errors=True)


def test_metrics_summary() -> None:
    _section("metrics")
    tmp = _make_tmp_copy()
    s = Store()
    m = Metrics(s)
    for i in range(20):
        m.record_cache(hit=(i % 3 == 0), latency_ms=1.0 + i * 0.1)
    summary = m.summary()
    assert summary["cache"]["samples"] == 20
    assert 0.0 < summary["cache"]["hit_rate"] < 1.0
    _ok(f"cache hit_rate={summary['cache']['hit_rate']} over 20 samples")
    shutil.rmtree(tmp, ignore_errors=True)


def test_doctor() -> None:
    _section("doctor")
    tmp = _make_tmp_copy()
    d = Doctor()
    rep = d.run()
    assert rep["sqlite"]["status"] == "ok"
    _ok("sqlite layer OK")
    # cloud layers should report disabled (no creds in test)
    for k in ("redis", "supabase", "r2"):
        assert rep[k]["status"] in ("disabled", "skipped")
    _ok("cloud layers disabled (expected)")
    assert rep["summary"]["healthy"]
    _ok("summary.healthy == true")
    shutil.rmtree(tmp, ignore_errors=True)


def test_memoria_facade() -> None:
    _section("Memoria facade")
    tmp = _make_tmp_copy()
    m = Memoria()
    m.set("test:key", "value", category="test", tags=["t1"])
    v = m.get("test:key")
    assert v and v["value"] == "value"
    _ok("set + get through facade")
    eid = m.explain("topic", "decision", rationale="why",
                    risk="downside", reward="upside", confidence=0.6)
    assert len(eid) == 32
    _ok(f"explain id={eid[:8]}")
    out = m.backup.local()
    assert Path(out["path"]).exists()
    _ok(f"backup.local wrote {out['path']}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_cache_degrades_gracefully() -> None:
    _section("cache graceful degradation")
    tmp = _make_tmp_copy()
    c = Cache()
    # Should not raise; should report not connected
    assert c.is_connected is False
    _ok("cache reports not connected")
    assert c.get("anything") is None
    _ok("cache.get returns None on miss (no exception)")
    assert c.put("k", "v") is False
    _ok("cache.put returns False when down")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_store_roundtrip,
        test_store_search,
        test_explanations,
        test_backup_local_and_restore,
        test_metrics_summary,
        test_doctor,
        test_memoria_facade,
        test_cache_degrades_gracefully,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{'='*40}\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())