# Risk vs Reward Analysis

A honest, layer-by-layer breakdown. Memoria is designed so that **local
SQLite is the canonical store and every other layer is a force
multiplier** — if all three cloud layers disappear tomorrow, the
system still works, just slower and without off-site backup.

The risk-reward summary at the bottom of this document is the one
you'd print and pin to a wall.

---

## Layer 1: SQLite (local canonical)

### Reward
- **Zero-dep**: ships in Python stdlib, no install.
- **Sub-ms latency**: typically 0.5-2 ms for `get/set`, even on 1 vCPU.
- **ACID**: full transactions, WAL mode, foreign keys, triggers.
- **FTS5 search**: built-in full-text search with ranked results.
- **Portable backup**: one `.db` file you can `scp`, `rsync`, or attach
  to an email.
- **Predictable cost**: $0 forever, no rate limits, no API quota.

### Risk
- **Single point of failure if not backed up** — disk dies, memory
  dies. **Mitigation:** `m.backup.local()` after every batch and
  `m.backup.r2()` nightly.
- **WAL file growth** — without `PRAGMA wal_checkpoint(TRUNCATE)` the
  `-wal` sidecar can grow. **Mitigation:** backup triggers implicit
  checkpoint; otherwise run `sqlite3 memoria.db 'PRAGMA wal_checkpoint;'`
  weekly.
- **No multi-writer** — SQLite serializes writes. Fine for a single
  agent or low-concurrency; bottleneck for many parallel writers.
  **Mitigation:** none needed at agent scale.
- **No native network access** — can't share between boxes. **Mitigation:**
  Supabase or R2 backup fills this gap.

### Bottom line
The risk-reward ratio is excellent. The only meaningful risk is
"you forgot to back up," and the system reminds you with the
audit_log table plus the `m.doctor.run()` health check.

---

## Layer 2: Redis (hot cache)

### Reward
- **Sub-millisecond reads** — typically 0.2-0.5 ms locally, 1-3 ms
  for Upstash over WAN.
- **Decouples read load from SQLite** — your canonical store is
  protected from thundering herds of `get()` calls.
- **Free tier is generous** — Upstash gives 10K commands/day and
  256 MB; for a personal agent that's months of headroom.
- **No schema migrations** — keys are opaque, you can change the
  value shape without a Redis-side migration.

### Risk
- **Cold-start cost on a fresh box** — cache is empty, every read
  misses once. **Mitigation:** acceptable; first read is the only one
  that's slow.
- **Stale data on cache-server issues** — if a write succeeds locally
  but the cache update fails, the cache can hold a stale value until
  TTL (default 1 hour). **Mitigation:** `delete()` also invalidates
  the cache; write-through is the default; on a real inconsistency,
  `Cache.invalidate_all()` clears the lot.
- **Credential leak** — Redis URL + token in `.env` gives full
  read/write to anyone who finds it. **Mitigation:** `chmod 600 .env`,
  scope tokens to a single Redis instance, never commit `.env`.
- **Cost escalation if abused** — a runaway script can burn through
  the free tier. **Mitigation:** monitor with `m.stats()['performance']`,
  set rate limits at the client, rotate the token if rate is hit.
- **Upstash project auto-pause** — if the Upstash dashboard is
  inactive for 7 days, the project can be paused. **Mitigation:**
  visit the dashboard monthly OR switch to a self-hosted Redis on
  the same VPS (recommended for full sovereignty).

### Bottom line
The risk-reward ratio is good for read-heavy workloads. If your
workload is mostly writes, skip Redis — the cache adds latency
without benefit.

---

## Layer 3: Supabase (durable cloud backup)

### Reward
- **500 MB free Postgres** — generous for an agent's memory.
- **REST API** — no need to manage a connection pool, just POST
  JSON.
- **Realtime subscriptions** — if you later want to watch memory
  changes from another process, Supabase emits them over WebSocket.
- **Auth + Storage included** — can be extended to multi-user
  agents without a separate auth service.

### Risk
- **Project pause on inactivity** — Supabase pauses free projects
  after 7 days of no API activity. **Mitigation:** nightly cron
  `m.backup.supabase()` keeps the project warm.
- **Vendor lock-in** — the `memories` and `explanations` tables
  use a generic schema, but if you add Supabase-specific features
  (auth, RLS, functions), migration is non-trivial. **Mitigation:**
  keep Memoria's data layer vanilla SQL; use Supabase features in
  a separate project if needed.
- **Cost escalation** — 500 MB sounds big, but if you attach images
  or long audio transcripts, you can hit the limit. **Mitigation:**
  keep the `value` column to small JSON; binary blobs go to R2.
- **PII in the free tier** — Supabase's free tier is not HIPAA/GDPR
  compliant for production. **Mitigation:** redact PII before
  `m.set()` if you handle personal data.
- **Outage** — Supabase has had 1-2 hour regional outages in the
  past. **Mitigation:** local layer keeps running; backup retries
  on next cron tick.
- **RLS not enforced by default** — service_role bypasses RLS, so a
  leaked key = full read/write/delete. **Mitigation:** restrict
  the service_role key to the Memoria IP, rotate quarterly, never
  expose it in a public repo.

### Bottom line
The risk-reward ratio is good for personal projects and side
agents. For production or multi-tenant, upgrade to Pro ($25/mo)
and enable RLS or switch to a self-hosted Postgres.

---

## Layer 4: Cloudflare R2 (cold archive)

### Reward
- **10 GB free egress-free** — R2's killer feature is **no egress
  fees**, so a restore that pulls 1 GB costs the same as 1 KB.
- **S3-compatible** — any S3 tool (`aws`, `s3cmd`, `mc`, `rclone`)
  can read/write the bucket.
- **Globally distributed** — Cloudflare's edge network puts the
  data close to wherever you restore from.
- **Cold-storage cost** — at $0.015/GB/month after the free tier,
  it's one of the cheapest S3 alternatives.
- **Survives total infra loss** — if the VPS dies, the bucket is
  intact. R2 is the "last line of defense" for Memoria.

### Risk
- **API token leak** — a leaked R2 token = read/write to one bucket.
  **Mitigation:** scope the token to a single bucket, restrict IPs
  if possible, rotate quarterly.
- **Bucket deletion** — accidental `wrangler r2 object delete` or
  dashboard mishap = total archive loss. **Mitigation:** enable
  bucket versioning in the R2 dashboard; object lock for write-once
  snapshots if you want true immutability.
- **Slow first upload** — R2 cold-start can take 30-60 s for the
  first PUT in a session. **Mitigation:** warm-up ping or accept
  the latency on the very first backup of the day.
- **Cost escalation** — beyond 10 GB or 10M reads, charges start.
  For an agent with 10K memories and nightly backups, you'd take
  years to hit this. **Mitigation:** set a Cloudflare billing alert
  at $1.

### Bottom line
The risk-reward ratio is excellent. R2 is the cheapest S3
alternative, the free tier is generous, and it's the only layer
that survives the loss of every other component.

---

## Cross-layer risks

### Schema drift
- **Risk:** local SQLite and Supabase schemas diverge if you add a
  column locally and forget to push the migration.
- **Mitigation:** `schema.sql` is the single source of truth. When
  you change it, run it in the Supabase SQL editor and reset the
  `schema_meta.schema_version` row.

### Backup retention
- **Risk:** `data/backups/` grows without bound.
- **Mitigation:** see `scripts/cleanup.sh` (or write a one-liner
  cron: `find ~/memoria/data/backups -mtime +30 -delete`).

### Clock skew
- **Risk:** `created_at` is `time.time() * 1000` from the local
  clock. If you move Memoria to a different timezone, sort order
  changes.
- **Mitigation:** sort by `created_at` only within a single node;
  use server-side `created_at` from Supabase for cross-node ordering.

### Concurrent processes
- **Risk:** two Memoria processes writing to the same SQLite file.
- **Mitigation:** WAL mode allows concurrent reads; writes still
  serialize. For a single agent, this is not an issue. If you run
  multiple agents, point each to its own `data/` directory.

---

## TL;DR Risk-Reward Matrix

| Layer    | Reward                                  | Risk                              | Verdict     |
|----------|-----------------------------------------|-----------------------------------|-------------|
| SQLite   | sub-ms, free, durable, FTS5             | no off-site backup by itself      | **USE**     |
| Redis    | sub-ms cache, free tier                 | stale on partial failure          | **OPTIONAL**|
| Supabase | durable cloud, REST, 500 MB free         | project pause on inactivity       | **OPTIONAL**|
| R2       | 10 GB free, no egress, total-loss-proof | API token leak                    | **STRONG OPTIONAL, recommended**|

## Decision tree

```
Start here
  │
  ├── Need multi-device sync? ──yes──▶ Enable Supabase
  │                                   │
  │                                   └── no ──▶ Skip Supabase
  │
  ├── Read-heavy workload (>10 reads / write)?
  │     │
  │     yes ──▶ Enable Redis
  │     │
  │     no  ──▶ Skip Redis
  │
  └── Care about total infra loss?
        │
        yes ──▶ Enable R2 (strongly recommended)
        │
        no  ──▶ Skip R2, rely on local backups
```

For a personal AI agent (Cuanology-shape): SQLite only is enough to
ship. For an agent that survives a VPS loss: SQLite + R2. For a
multi-device or multi-agent setup: SQLite + Redis + Supabase + R2
(all four).

## When to disable a layer

- **Disable Redis** if your read:write ratio is below 5:1 — the
  cache adds complexity without enough benefit.
- **Disable Supabase** if you store nothing important enough to
  survive a local disk failure (e.g. ephemeral session notes).
- **Disable R2** if your data is reproducible from upstream sources
  (e.g. you're just caching API responses). Otherwise keep it on.

## When to upgrade tiers

- **Redis (Upstash)**: 10K cmd/day ≈ 50 reads/min sustained. If you
  exceed that, switch to a self-hosted Redis on the same VPS — it's
  often cheaper and removes the network RTT.
- **Supabase**: 500 MB is enough for ~100K small JSON memories. Hit
  the limit → upgrade to Pro ($25/mo, 8 GB) or migrate the durable
  layer to your own Postgres.
- **R2**: 10 GB holds ~5M small JSON memories. Beyond that, R2 costs
  $0.015/GB/month, so 100 GB is $1.35/month — basically free.

## Final note

This document is a living artifact. When you discover a new risk
or a reward turns out to be larger (or smaller) than expected,
update this file and commit. Memoria's value is being honest
about what each layer can and cannot do for you.