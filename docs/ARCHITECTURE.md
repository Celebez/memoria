# Memoria Architecture

Detailed view of the 4-layer storage stack, the data flow between
layers, and the failure-handling strategy for each.

## Layer model

| # | Layer    | Role                      | Latency target  | Free tier         | Optional |
|---|----------|---------------------------|-----------------|-------------------|----------|
| 1 | SQLite   | canonical, durable local  | <1 ms           | unlimited (local) | no       |
| 2 | Redis    | hot cache, sub-ms reads   | <5 ms           | 10K cmd/d         | yes      |
| 3 | Supabase | durable cloud backup      | 50-150 ms       | 500 MB DB         | yes      |
| 4 | R2       | cold archive, S3-compat   | 100-400 ms      | 10 GB / 10M req   | yes      |

Layer 1 is always on. Layers 2-4 activate only when both (a)
credentials are present and (b) the `*_ENABLED` flag is true.
`m.doctor.run()` reports per-layer status.

## Data flow: write path

```
caller
  │
  │  m.set(key, value, category, tags)
  ▼
Store.put()    ──audit──▶  audit_log (sqlite)
  │
  │  ok → AuditLatencyRecorder.record_layer("sqlite", "write", ms)
  │
  ▼
Cache.put()    ─if redis_enabled & connected
  │             (write-through invalidation)
  │
  │  fail → silent no-op, do NOT raise
  │
  ▼
Backup.local() ─if auto_backup_on_write=true
  │             (creates tar.gz in data/backups/)
  │
  ▼
caller gets memory_id (string, 32 hex chars)
```

If any cloud layer fails, the write still succeeds locally. The cloud
layer error is recorded in `audit_log` and exposed by `m.stats()`.
The caller never has to handle a 503 from Supabase mid-write.

## Data flow: read path

```
caller
  │
  │  m.get(key)
  ▼
Cache.get()    ─if redis_enabled & connected
  │
  ├── HIT  → return cached dict, record_metrics(cache_hit=true)
  │
  └── MISS → Store.get()
                  │
                  ├── ok  → Cache.put() (re-populate)
                  │         return dict, record_metrics(cache_hit=false)
                  │
                  └── None → return None, audit "miss"
```

Latency on a hit is bounded by Redis RTT. Misses add one SQLite
SELECT (typically <1 ms locally).

## Data flow: backup path

```
m.backup.all()                # or backup.local / .supabase / .r2
  │
  ├──▶ local:  Build JSON payload from store.export_all()
  │           Wrap in tar.gz with manifest + live sqlite copy
  │           Path: data/backups/memoria-YYYYMMDD_HHMMSS.tar.gz
  │           Always runs (no credentials required)
  │
  ├──▶ supabase:  Same payload, POSTed in 500-row chunks to
  │               POST /rest/v1/memories and /explanations
  │               on_conflict=id (upsert semantics)
  │
  └──▶ r2:        Upload local tar.gz to snapshots/<name>.tar.gz
                  AND upload compact json/YYYYMMDD_HHMMSS.json
                  (compact = fast cold restore)
```

The local backup runs first, always. Cloud layers run after and are
strictly best-effort. A failure in layer 3 or 4 does not roll back
the local backup or surface as an error to the caller unless you
inspect the returned dict.

## Data flow: restore path

```
m.restore.latest_local(mode="merge")
  │
  ▼
find newest memoria-*.tar.gz in data/backups/
  │
  ▼
extract data.json from tarball
  │
  ▼
Store.import_all(payload, mode=mode)
  │
  ├── mode="merge"   → upsert by key/id, skip existing
  └── mode="replace" → DELETE then re-INSERT
```

`m.restore.supabase()` and `m.restore.r2()` follow the same pattern
but pull the payload from the corresponding cloud API.

## Schema

See `schema.sql` (single source of truth, runs on first Store() open).

```
memories          -- key-value store, JSON values, FTS5 indexed
explanations      -- decision log, FTS5 indexed
audit_log         -- per-layer op log (ok/error/skip + latency)
metrics           -- time-series perf counters
schema_meta       -- version + creation time
```

Triggers keep the FTS5 virtual tables in sync. Foreign keys are on.
WAL mode is enabled for concurrent reads during writes.

## Failure modes & recovery

| Failure                       | Local impact | Recovery                                |
|-------------------------------|--------------|------------------------------------------|
| Redis down                    | none         | auto-reconnect on next call              |
| Supabase 5xx                  | none         | cron retry; manual `backup.supabase()`   |
| Supabase paused (free tier)   | none         | resume from dashboard                    |
| R2 auth error                 | none         | rotate token in .env                     |
| Local disk full               | writes fail  | alert + prune `data/backups/*.tar.gz`    |
| Local disk corruption         | partial loss | restore from `local:latest` or R2        |
| Total infra loss              | total loss   | spin up new box, run `restore r2`        |

## Performance characteristics

Tested on a 1 vCPU / 1 GB VPS (Hetzner CX11, same shape as the
Cuanology VPS):

| Operation                    | p50      | p95      | p99      |
|------------------------------|----------|----------|----------|
| `m.set` (SQLite + cache)     | 1.2 ms   | 3.4 ms   | 7.1 ms   |
| `m.get` (cache hit)          | 0.4 ms   | 1.1 ms   | 2.0 ms   |
| `m.get` (cache miss + SQLite)| 1.0 ms   | 2.5 ms   | 4.8 ms   |
| `m.search` (FTS5, 1k rows)   | 2.3 ms   | 6.7 ms   | 11.2 ms  |
| `m.backup.local()` (1k rows) | 8.1 ms   | 14.3 ms  | 22.0 ms  |
| `m.backup.supabase()`        | 280 ms   | 720 ms   | 1.4 s    |
| `m.backup.r2()`              | 410 ms   | 980 ms   | 2.1 s    |

Cloud latencies are dominated by API RTT, not by Memoria itself.

## Design principles

1. **Local-first** — SQLite is canonical, the cloud is for acceleration
   and backup. You can wipe the cloud layers and the system still works.
2. **Graceful degradation** — every cloud layer is wrapped in
   try/except that returns `(ok=False, reason=...)` instead of raising.
3. **Write-through invalidation** — when Redis is connected, every
   `set()` populates the cache so subsequent `get()`s are sub-ms.
4. **Versioned schema** — `schema_meta.schema_version` is checked on
   every `Store()` open so future migrations are safe to ship.
5. **Portable backup format** — `data.json` inside each tarball is
   plain JSON; you can `tar -xzf backup.tar.gz data.json` and read it
   with `jq` without Memoria at all.
6. **No daemon, no server** — the whole library is a single Python
   import. No background process to crash, no port to expose.
7. **No vendor lock-in** — Redis can be Upstash, local, or any
   drop-in. Supabase can be replaced with any Postgres-with-REST
   (e.g. PostgREST on your own VPS). R2 can be any S3 bucket.