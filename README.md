# Memoria

A local-first, multi-layer memory + decision-explanation system for AI agents.
Stores everything in SQLite by default; accelerates reads with **Redis**,
backs up durably to **Supabase**, and archives cold snapshots to
**Cloudflare R2**. Any cloud layer is optional — the system runs at
full speed on a fresh box with only SQLite.

```
┌──────────────────────────────────────────────────────────┐
│  local SQLite  ──read──▶  Redis cache  ──fallback──▶  Supabase   ──archive──▶  R2  │
│  (canonical)        (hot)                     (durable)              (cold)       │
└──────────────────────────────────────────────────────────┘
```

> **Canonical source policy.** This repository is a published artifact, not
> a collaborative project. Issues, PRs, and wiki are disabled. Documentation
> is licensed under **CC BY-ND 4.0** (no derivatives). See [`NOTICE.md`](NOTICE.md)
> for the full policy and the canonical URL.
>
> **Code** (Python, shell, SQL) remains under the **MIT License** —
> see [`LICENSE`](LICENSE).

## Documentation

- **[Tutorial lengkap (Indonesia)](docs/TUTORIAL.md)** — step-by-step dari nol sampai production, cocok untuk di-share ke publik.
- **[Strategic Value](docs/STRATEGIC-VALUE.md)** — kenapa memory system penting + analisa token cost kalau kebanyakan tool (bilingual, shareable).
- **[Architecture](docs/ARCHITECTURE.md)** — diagram alur data, performance benchmarks, design principles.
- **[Risk vs Reward](docs/RISK_REWARD.md)** — analisis jujur per layer: kapan perlu, kapan tidak, kapan upgrade.

## Why

Most memory layers for agents are either:
- A single remote DB (Supabase, Notion, etc.) — fast to set up, dies when
  the API is down or the project is paused.
- Plain markdown files — works locally, no search, no performance data,
  no backup.

Memoria gives you both: **local-first, cloud-optional, 4-layer durability,
FTS5 search, and a decision-explanation log** that captures the
*why* behind every important choice (rationale + risk + reward +
confidence).

## Quick Start (5 minutes)

### 1. Install

```bash
git clone https://github.com/Celebez/memoria.git ~/memoria
cd ~/memoria
pip install -r requirements.txt   # only `requests` and `redis` are mandatory
```

`boto3` is only needed if you want to use Cloudflare R2.

### 2. Try it (zero config, just SQLite)

```bash
./bin/memoria doctor
```

```json
{
  "config": { "sqlite": ".../data/memoria.db", "layers_active": ["sqlite"] },
  "sqlite": { "status": "ok", "memories": 0, "explanations": 0 },
  "redis":   { "status": "disabled" },
  "supabase":{ "status": "disabled" },
  "r2":      { "status": "disabled" },
  "summary": { "healthy": true }
}
```

### 3. Write your first memory

```bash
./bin/memoria set user:pref:theme '"dark"' --category ui --tag pref --tag ui
./bin/memoria get user:pref:theme
./bin/memoria search "theme"
```

### 4. Record a decision (explanation log)

```bash
./bin/memoria explain \
  "deploy strategy" \
  "rolling update" \
  --rationale "zero downtime for users" \
  --risk      "rollback is more complex" \
  --reward    "no maintenance window required" \
  --confidence 0.85
```

This writes to SQLite **and** saves a human-readable mirror to
`data/explanations/<id>-<topic>.md`.

### 5. Backup

```bash
./bin/memoria backup           # alias for 'backup local'
./bin/memoria backup all       # local + supabase + r2 (if configured)
ls data/backups/               # memoria-YYYYMMDD_HHMMSS.tar.gz
```

### 6. Restore (e.g. on a fresh VPS)

```bash
./bin/memoria restore local:latest    # most recent local archive
./bin/memoria restore supabase        # pull from Supabase
./bin/memoria restore r2              # pull latest JSON from R2
```

### 7. Inspect performance

```bash
./bin/memoria stats
```

```json
{
  "store":   { "memories": 4, "explanations": 2, "db_size_bytes": 118784 },
  "explanations": { "avg_confidence": 0.775, "by_outcome": {...} },
  "performance":  { "cache": {...}, "latency": {...}, "by_layer": {...} }
}
```

## Python API

```python
from memoria import Memoria

m = Memoria()

# ----- memories -----
m.set("user:name", "larazati", category="profile", tags=["user"])
v = m.get("user:name")                 # {id, key, value, category, tags, ...}
m.delete("user:name")
m.list_keys(category="ui", limit=50)
m.search("rolling update")             # FTS5

# ----- decision explanations -----
m.explain(
    topic="cache strategy",
    decision="write-through invalidation",
    rationale="simpler than TTL, eliminates stale reads",
    risk="write amplification, more Redis traffic",
    reward="always-fresh reads, simpler mental model",
    confidence=0.9,
)
m.list_explanations(topic="cache strategy")
m.search_explanations("stale")
m.record_outcome(exp_id, "success")    # close the loop later

# ----- backup / restore -----
m.backup.local()                       # always works
m.backup.supabase()                    # needs SUPABASE_ENABLED=true
m.backup.r2()                          # needs R2_ENABLED=true
m.backup.all()                         # all configured layers

m.restore.local("/path/to/memoria-20260621_064812.tar.gz")
m.restore.latest_local(mode="merge")  # mode=merge|replace
m.restore.supabase(mode="merge")
m.restore.r2(mode="merge")

# ----- health + performance -----
print(m.doctor.run())                  # 4-layer health report
print(m.stats())                       # dashboard
```

## Enabling cloud layers (optional)

All three are **free tier** and disabled by default. Enable one or all
three by editing `.env` (copy `.env.example` first).

### Redis (hot cache)

| Provider     | Free tier          | Setup                            |
|--------------|--------------------|----------------------------------|
| Local        | unlimited          | `redis-server` already installed |
| Upstash      | 10K cmd/day, 256MB | https://upstash.com              |

```bash
# .env
REDIS_ENABLED=true
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
# or for Upstash REST:
# REDIS_URL=rediss://default:***@***.upstash.io:6379
```

### Supabase (durable backup)

1. Create a free project at https://supabase.com (500 MB database).
2. Open **SQL Editor** and run the entire `schema.sql` file.
3. Copy the **service_role** key from Project Settings → API.

```bash
# .env
SUPABASE_ENABLED=true
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
```

### Cloudflare R2 (cold archive)

1. Create a free R2 bucket at https://dash.cloudflare.com (10 GB).
2. Generate an API token with **Object Read & Write** scope.
3. Note your **Account ID** from the R2 dashboard.

```bash
# .env
R2_ENABLED=true
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=memoria-backups
# R2_ENDPOINT=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com  (auto)
```

After editing `.env`, run `./bin/memoria doctor` again — the layer
status should change from `disabled` to `ok` (or `error` with a
helpful message if something is off).

## Architecture in one diagram

```
   writes ──────▶  SQLite  ──────▶  Redis (write-through)
                      │
                      ├──── nightly tar.gz  ──▶  data/backups/
                      ├──── on-demand push  ──▶  Supabase (REST)
                      └──── on-demand push  ──▶  R2 (S3-compatible)
   reads  ◀──────  Redis hit ──┐
                               └── miss → SQLite
```

See `docs/ARCHITECTURE.md` for the long version and
`docs/RISK_REWARD.md` for the full risk-vs-reward breakdown.

## Testing

```bash
cd ~/memoria
python3 tests/test_memoria.py
```

8 tests, all should print `8 passed, 0 failed`. Tests use temp
directories so they don't touch your real `data/`.

## Repo map

```
memoria/
├── memoria/                  # Python package
│   ├── config.py             # env-driven config
│   ├── store.py              # SQLite canonical store (FTS5)
│   ├── cache.py              # Redis write-through cache
│   ├── cloud.py              # Supabase REST + R2 (S3) clients
│   ├── explanations.py       # decision logger + md mirror
│   ├── metrics.py            # latency + hit-rate tracker
│   ├── backup.py             # snapshot to local/supabase/r2
│   ├── restore.py            # restore from any layer
│   ├── doctor.py             # health check
│   └── memoria.py            # unified facade
├── bin/memoria               # CLI entry point
├── schema.sql                # SQLite (also compatible with Supabase)
├── tests/test_memoria.py     # 8 self-contained tests
├── docs/
│   ├── ARCHITECTURE.md       # detailed architecture
│   └── RISK_REWARD.md        # risk vs reward per layer
├── .env.example              # credential template
├── requirements.txt
├── .gitignore
└── README.md                 # you are here
```

## FAQ

**Q: Can I use Memoria without any cloud credentials?**
A: Yes. SQLite is the canonical store and works fully offline. Redis,
Supabase, and R2 only activate when you fill in their credentials and
set `*_ENABLED=true`.

**Q: How do I migrate from another memory system?**
A: Write a one-time import script that calls `m.set("legacy:...", value)`
for each row. The `restore.local()` path also accepts any tar.gz
containing a `data.json` with the same schema.

**Q: What happens if my VPS dies and Supabase is paused?**
A: Local backups under `data/backups/` are independent of Supabase.
You can also pre-stage R2 with `m.backup.r2()` while the box is up —
that's the layer that survives total infra loss.

**Q: Is this safe to use in production?**
A: The local layer has WAL mode and a backup-on-write option
(`AUTO_BACKUP_ON_WRITE=true`). The cloud layers are best-effort and
never block the data path. See `docs/RISK_REWARD.md` for the full
breakdown.

**Q: How does the explanation log work?**
A: Each `m.explain()` writes one row to `explanations` (SQLite, FTS5
indexed) and a matching markdown file to `data/explanations/`. You
can later call `m.record_outcome(id, "success" | "partial" | "fail")`
to close the feedback loop.

## License

MIT — see `LICENSE`.

## Credits

By Celebez.
Inspired by the "explanation ledger" pattern from the Hermes memory
system and the "local-first, cloud-optional" approach from CouchDB's
sync model.