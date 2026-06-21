# Tutorial: Sistem Memori + Penjelasan Keputusan untuk AI Agent

Tutorial lengkap step-by-step untuk membangun sistem memori lokal-first
berlapis (4 layer) plus explanation logger untuk AI agent. Dirancang
supaya:

- **Jalan tanpa cloud** — SQLite cukup untuk start, semua layer lain
  opt-in.
- **Gratis** — semua layer memakai free tier yang generous.
- **Portable** — backup dalam format JSON + tar.gz, bisa di-restore
  tanpa software khusus.
- **Auditable** — setiap keputusan penting dicatat dengan rationale,
  risk, reward, dan confidence-nya.

Target pembaca: developer Python level menengah yang sudah pernah
bangun CLI tools dan familiar dengan REST API.

---

## Daftar Isi

1. [Konsep Dasar](#1-konsep-dasar)
2. [Prasyarat](#2-prasyarat)
3. [Setup Project](#3-setup-project)
4. [Layer 1: SQLite (Canonical Store)](#4-layer-1-sqlite-canonical-store)
5. [Layer 2: Redis (Hot Cache)](#5-layer-2-redis-hot-cache)
6. [Layer 3: Supabase (Durable Backup)](#6-layer-3-supabase-durable-backup)
7. [Layer 4: Cloudflare R2 (Cold Archive)](#7-layer-4-cloudflare-r2-cold-archive)
8. [Explanation Logger](#8-explanation-logger)
9. [Performance Metrics](#9-performance-metrics)
10. [Backup & Restore](#10-backup--restore)
11. [CLI](#11-cli)
12. [Cron Scheduling](#12-cron-scheduling)
13. [Testing](#13-testing)
14. [Production Checklist](#14-production-checklist)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Konsep Dasar

### Kenapa perlu sistem memori berlapis?

Satu database saja tidak cukup untuk agent yang berjalan 24/7:

- **Single remote DB** (Supabase, Notion, Firebase) — cepat setup,
  tapi mati kalau API down atau project di-pause.
- **Local-only** (SQLite, JSON file) — cepat, tapi hilang kalau
  disk rusak atau VPS hilang.
- **Cache-only** (Redis) — cepat, tapi volatile.

Solusinya: **local-first, cloud-optional**. SQLite jadi sumber
kebenaran (canonical), Redis percepat reads, Supabase jadi backup
durable, R2 jadi arsip dingin. Matikan semua cloud layer dan
sistem tetap jalan.

### Kenapa perlu explanation logger?

AI agent membuat banyak keputusan. Tanpa audit trail:

- Tidak tahu **kenapa** keputusan itu dibuat.
- Tidak bisa belajar dari kesalahan masa lalu.
- Tidak bisa menjelaskan ke user apa yang sebenarnya terjadi.

Explanation logger menjawab: apa topiknya, keputusan apa yang
diambil, kenapa, risiko apa, reward apa, dan seberapa yakin
agent saat itu. Setiap entry juga punya outcome yang diisi
kemudian untuk close the feedback loop.

### Arsitektur 4 layer

```
┌──────────────────────────────────────────────────────────┐
│  local SQLite  ──read──▶  Redis cache  ──fallback──▶     │
│  (canonical)        (hot)                                │
│      │                                                    │
│      ├──── nightly tar.gz  ──▶  data/backups/            │
│      ├──── on-demand push  ──▶  Supabase (REST)         │
│      └──── on-demand push  ──▶  R2 (S3-compatible)      │
└──────────────────────────────────────────────────────────┘
```

Layer 1 selalu aktif. Layer 2-4 aktif hanya jika kredensial diisi
dan flag `*_ENABLED=true` diaktifkan.

---

## 2. Prasyarat

### Software

- Python 3.10 atau lebih baru
- pip (sudah termasuk di Python modern)
- Git
- Optional: `redis-server` untuk Redis lokal
- Optional: `boto3` Python package untuk Cloudflare R2

### Akun (semua optional, semua free tier)

| Layanan       | Free tier          | Alamat                |
|---------------|--------------------|------------------------|
| Redis lokal   | unlimited          | install sendiri         |
| Upstash Redis | 10K cmd/d, 256MB   | https://upstash.com    |
| Supabase      | 500 MB Postgres    | https://supabase.com   |
| Cloudflare R2 | 10 GB, no egress   | https://dash.cloudflare.com |

### Cek cepat

```bash
python3 --version          # 3.10+
pip --version
git --version
```

---

## 3. Setup Project

### 3.1 Clone repository

```bash
git clone https://github.com/<your-username>/memoria.git
cd memoria
```

### 3.2 Install dependencies

```bash
pip install -r requirements.txt
```

File `requirements.txt`:

```
requests>=2.31
redis>=5.0
boto3>=1.34      # opsional, hanya untuk R2
```

### 3.3 Verifikasi instalasi

```bash
./bin/memoria doctor
```

Output yang diharapkan (semua layer cloud `disabled` di awal):

```json
{
  "config": { "layers_active": ["sqlite"] },
  "sqlite": { "status": "ok", "memories": 0, "explanations": 0 },
  "redis": { "status": "disabled" },
  "supabase": { "status": "disabled" },
  "r2": { "status": "disabled" },
  "summary": { "healthy": true }
}
```

Sistem langsung jalan dengan SQLite. Folder `data/` akan dibuat
otomatis.

### 3.4 Struktur direktori

```
memoria/
├── memoria/                  # package Python utama
│   ├── config.py             # env-driven configuration
│   ├── store.py              # SQLite canonical store
│   ├── cache.py              # Redis write-through cache
│   ├── cloud.py              # Supabase + R2 clients
│   ├── explanations.py       # decision logger
│   ├── metrics.py            # performance tracking
│   ├── backup.py             # orchestrator
│   ├── restore.py            # restore logic
│   ├── doctor.py             # health check
│   └── memoria.py            # unified facade
├── bin/memoria               # CLI entry point
├── schema.sql                # SQLite + Supabase schema
├── tests/test_memoria.py     # 8 unit tests
├── docs/                     # dokumentasi
├── scripts/                  # cron installer, cleanup
├── data/                     # runtime (auto-created)
├── .env.example              # credential template
├── requirements.txt
├── README.md
└── LICENSE
```

---

## 4. Layer 1: SQLite (Canonical Store)

SQLite adalah satu-satunya layer yang **wajib**. Ia menyimpan
semua data secara durable di lokal, dengan performa sub-ms untuk
operasional agent.

### 4.1 Schema

Schema single source of truth disimpan di `schema.sql` dan di-load
otomatis saat `Store()` di-instantiate:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,          -- uuid hex
    key          TEXT UNIQUE NOT NULL,       -- user-defined
    value        TEXT NOT NULL,              -- JSON
    category     TEXT DEFAULT 'general',
    tags         TEXT DEFAULT '',            -- comma-separated
    source       TEXT DEFAULT 'local',
    access_count INTEGER DEFAULT 0,
    created_at   INTEGER NOT NULL,           -- epoch ms
    updated_at   INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    key, value, category, tags,
    content='memories', content_rowid='rowid'
);

-- (triggers untuk sinkronisasi FTS5, explanations table,
--  audit_log, metrics — lihat schema.sql lengkap)
```

### 4.2 Class Store

```python
import sqlite3, json, time, uuid
from contextlib import contextmanager
from pathlib import Path

class Store:
    def __init__(self, db_path="data/memoria.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
        finally:
            c.close()

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(Path("schema.sql").read_text())

    def put(self, key, value, category="general", tags=None):
        """Insert or update. Returns the id."""
        ts = int(time.time() * 1000)
        tags_csv = ",".join(tags or [])
        value_json = json.dumps(value, ensure_ascii=False, default=str)
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
                return existing["id"]
            memory_id = uuid.uuid4().hex
            c.execute(
                "INSERT INTO memories(id, key, value, category, tags, source,"
                " access_count, created_at, updated_at)"
                " VALUES (?,?,?,?,?, 'local', 0, ?, ?)",
                (memory_id, key, value_json, category, tags_csv, ts, ts),
            )
            return memory_id

    def get(self, key):
        """Fetch by key. Returns dict or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM memories WHERE key = ?", (key,)
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE memories SET access_count = access_count + 1"
                    " WHERE id = ?", (row["id"],)
                )
                row = c.execute(
                    "SELECT * FROM memories WHERE id = ?", (row["id"],)
                ).fetchone()
        if row:
            d = dict(row)
            d["value"] = json.loads(d["value"])
            return d
        return None

    def search(self, query, limit=20):
        """Full-text search with FTS5 ranking."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT m.*, rank FROM memories_fts f"
                " JOIN memories m ON m.rowid = f.rowid"
                " WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["value"] = json.loads(d["value"])
            out.append(d)
        return out
```

### 4.3 Contoh penggunaan

```python
from memoria import Memoria
m = Memoria()

# Tulis memory
m.set("user:name", "alice", category="profile", tags=["user"])
m.set("user:pref:theme", "dark", category="ui")

# Baca
v = m.get("user:name")
# {'id': '...', 'key': 'user:name', 'value': 'alice', 'category': 'profile', ...}

# Search
results = m.search("theme")
# [{'key': 'user:pref:theme', 'value': 'dark', 'rank': ...}]
```

### 4.4 Kenapa FTS5?

FTS5 (Full-Text Search) bawaan SQLite mendukung:
- Tokenization otomatis
- Ranking berdasarkan relevance
- Boolean queries (`memoria_fts MATCH 'theme OR dark'`)
- Prefix queries (`memoria_fts MATCH 'roll*'`)

Untuk agent yang sering search lewat keyword, FTS5 lebih cepat dan
lebih akurat dibanding `LIKE '%...%'`.

---

## 5. Layer 2: Redis (Hot Cache)

Redis adalah cache in-memory yang mempercepat reads. Konsepnya:
**write-through** — setiap `set()` ke SQLite juga `set()` ke Redis
(jika Redis aktif dan terhubung). Reads pertama cek Redis, kalau
miss baru ke SQLite.

### 5.1 Kapan perlu Redis?

- Read-heavy workload (lebih dari 10 reads per write)
- Latency budget ketat (<5ms untuk `get()`)
- Multi-instance agent yang share cache

Kalau agent Anda melakukan <10 reads per write, lewati layer ini
— kompleksitasnya tidak sebanding benefitnya.

### 5.2 Setup Redis

**Opsi A: Local Redis (recommended untuk single VPS)**

```bash
sudo apt install redis-server
sudo systemctl enable --now redis-server
redis-cli ping    # harus return PONG
```

**Opsi B: Upstash (managed, free tier 10K cmd/day)**

1. Buka https://upstash.com dan buat akun.
2. Buat database baru, pilih region terdekat.
3. Copy **REST URL** dan **REST Token**.

### 5.3 Konfigurasi `.env`

```bash
REDIS_ENABLED=true
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
# REDIS_PASSWORD=...   # jika Redis pakai password
# Untuk Upstash REST:
# REDIS_URL=rediss://default:***@***.upstash.io:6379
```

### 5.4 Class Cache

```python
import json

try:
    import redis as _redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

class Cache:
    def __init__(self, host="127.0.0.1", port=6379, password="",
                 url="", ttl=3600, enabled=False):
        self.ttl = ttl
        self._client = None
        self._connected = False
        if not (enabled and REDIS_AVAILABLE):
            return
        try:
            if url:
                self._client = _redis.Redis.from_url(
                    url, decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                )
            else:
                self._client = _redis.Redis(
                    host=host, port=port, password=password or None,
                    decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                )
            self._client.ping()
            self._connected = True
        except Exception:
            self._client = None
            self._connected = False

    @property
    def is_connected(self):
        return self._connected

    def get(self, key):
        if not self._connected:
            return None
        try:
            raw = self._client.get(f"memoria:m:{key}")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def put(self, key, value, ttl=None):
        if not self._connected:
            return False
        try:
            self._client.set(
                f"memoria:m:{key}",
                json.dumps(value, default=str),
                ex=ttl or self.ttl,
            )
            return True
        except Exception:
            return False

    def delete(self, key):
        if not self._connected:
            return False
        try:
            self._client.delete(f"memoria:m:{key}")
            return True
        except Exception:
            return False
```

### 5.5 Pola write-through

```python
class Memoria:
    def set(self, key, value, **kwargs):
        # Tulis ke SQLite dulu (canonical)
        memory_id = self.store.put(key, value, **kwargs)
        # Lalu populate cache
        if self.cache.is_connected:
            self.cache.put(key, {"id": memory_id, "key": key,
                                 "value": value, **kwargs})
        return memory_id

    def get(self, key):
        # Coba cache dulu
        if self.cache.is_connected:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        # Miss -> ke SQLite
        result = self.store.get(key)
        if result and self.cache.is_connected:
            self.cache.put(key, result)
        return result
```

### 5.6 Graceful degradation

Class `Cache` **tidak pernah raise exception**. Kalau Redis down
atau kredensial salah, semua method jadi silent no-op:

```python
cache = Cache(enabled=True, host="wrong-host")
print(cache.is_connected)  # False
print(cache.get("any"))    # None (no exception)
print(cache.put("k", "v")) # False (no exception)
```

Sistem tetap jalan pakai SQLite saja.

---

## 6. Layer 3: Supabase (Durable Backup)

Supabase adalah Postgres managed dengan REST API gratis (500MB).
Layer ini mem-backup data ke cloud secara periodik untuk
ketahanan terhadap kehilangan lokal.

### 6.1 Setup Supabase

1. Buka https://supabase.com dan buat akun.
2. **New project** — pilih region terdekat dengan VPS Anda.
3. Tunggu provisioning selesai (~2 menit).
4. Buka **SQL Editor** dan paste seluruh isi `schema.sql`,
   lalu klik **Run**. Ini akan membuat tabel `memories`,
   `explanations`, dan view FTS5.
5. Buka **Settings → API** dan copy:
   - **Project URL** (contoh: `https://abc.supabase.co`)
   - **service_role** key (JANGAN anon key — butuh write access)

### 6.2 Konfigurasi `.env`

```bash
SUPABASE_ENABLED=true
SUPABASE_URL=https://abc.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### 6.3 Class Cloud (bagian Supabase)

```python
import requests

class Cloud:
    def __init__(self, url="", key=""):
        self.url = url
        self.key = key
        self._session = None
        self._tables_ok = {}

    def _session_get(self):
        if not (self.url and self.key):
            return None
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            })
        return self._session

    def _table_exists(self, name):
        if name in self._tables_ok:
            return self._tables_ok[name]
        sess = self._session_get()
        if not sess:
            return False
        try:
            r = sess.get(f"{self.url}/rest/v1/{name}?select=id&limit=1",
                         timeout=5)
            ok = r.status_code == 200
            self._tables_ok[name] = ok
            return ok
        except requests.RequestException:
            return False

    def upsert(self, table, rows, on_conflict="id"):
        """Batch upsert. Returns (ok, message)."""
        sess = self._session_get()
        if not sess:
            return False, "supabase not configured"
        if not self._table_exists(table):
            return False, f"table '{table}' not found"
        try:
            r = sess.post(
                f"{self.url}/rest/v1/{table}",
                params={"on_conflict": on_conflict},
                json=rows,
                timeout=30,
            )
            if r.status_code in (200, 201):
                return True, f"upserted {len(rows)} rows"
            return False, f"http {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            return False, str(e)

    def fetch_all(self, table, limit=10000):
        sess = self._session_get()
        if not sess:
            return False, "supabase not configured"
        if not self._table_exists(table):
            return False, f"table '{table}' not found"
        try:
            r = sess.get(
                f"{self.url}/rest/v1/{table}",
                params={"select": "*", "limit": str(limit)},
                timeout=30,
            )
            if r.status_code == 200:
                return True, r.json()
            return False, f"http {r.status_code}"
        except requests.RequestException as e:
            return False, str(e)
```

### 6.4 Backup ke Supabase

```python
def backup_supabase(m: Memoria):
    """Push all memories and explanations to Supabase."""
    payload = m.store.export_all()
    mem_rows = [
        {
            "id": row["id"], "key": row["key"],
            "value": json.dumps(row["value"], default=str)
                    if not isinstance(row["value"], str)
                    else row["value"],
            "category": row.get("category", "general"),
            "tags": row.get("tags", ""),
            "source": row.get("source", "local"),
            "access_count": row.get("access_count", 0),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
        for row in payload["memories"]
    ]
    # Batch in chunks of 500 to stay under payload limits
    for i in range(0, len(mem_rows), 500):
        ok, msg = m.cloud.upsert("memories", mem_rows[i:i+500])
        if not ok:
            return {"ok": False, "error": msg}
    return {"ok": True, "rows": len(mem_rows)}
```

### 6.5 Restore dari Supabase

```python
def restore_supabase(m: Memoria, mode="merge"):
    ok_m, mem = m.cloud.fetch_all("memories")
    if not ok_m:
        return {"ok": False, "error": mem}
    n = m.store.import_all(
        {"version": 1, "memories": mem, "explanations": []},
        mode=mode,
    )
    return {"ok": True, "imported": n}
```

---

## 7. Layer 4: Cloudflare R2 (Cold Archive)

R2 adalah object storage S3-compatible dari Cloudflare dengan
**10GB free** dan **no egress fees**. Cocok untuk arsip backup
yang jarang di-restore tapi harus tahan lama.

### 7.1 Setup R2

1. Buka https://dash.cloudflare.com dan login.
2. Pilih **R2 → Create bucket**, beri nama (mis.
   `memoria-backups-anda`).
3. Buka **R2 → Manage R2 API Tokens → Create API Token**:
   - Permissions: **Object Read & Write**
   - Bucket: pilih bucket Anda
   - TTL: sesuai kebutuhan
4. Copy **Account ID**, **Access Key ID**, dan **Secret Access Key**.

### 7.2 Konfigurasi `.env`

```bash
R2_ENABLED=true
R2_ACCOUNT_ID=your_account_id_here
R2_ACCESS_KEY_ID=your_access_key
R2_SECRET_ACCESS_KEY=your_secret_key
R2_BUCKET=memoria-backups-anda
# R2_ENDPOINT akan auto-generate dari R2_ACCOUNT_ID
```

### 7.3 Class Cloud (bagian R2)

```python
class Cloud:
    # ... (bagian Supabase di atas)

    def r2_upload(self, key, data, content_type="application/octet-stream"):
        import boto3
        if not (self.r2_account_id and self.r2_access_key and self.r2_secret_key):
            return False, "r2 not configured"
        endpoint = (self.r2_endpoint
                   or f"https://{self.r2_account_id}.r2.cloudflarestorage.com")
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=self.r2_access_key,
                aws_secret_access_key=self.r2_secret_key,
                region_name="auto",
            )
            client.put_object(
                Bucket=self.r2_bucket, Key=key,
                Body=data, ContentType=content_type,
            )
            return True, f"uploaded {len(data)} bytes to r2://{self.r2_bucket}/{key}"
        except Exception as e:
            return False, str(e)

    def r2_download(self, key):
        import boto3
        try:
            client = boto3.client(
                "s3",
                endpoint_url=f"https://{self.r2_account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=self.r2_access_key,
                aws_secret_access_key=self.r2_secret_key,
                region_name="auto",
            )
            obj = client.get_object(Bucket=self.r2_bucket, Key=key)
            return True, obj["Body"].read()
        except Exception as e:
            return False, str(e)
```

### 7.4 Backup ke R2

Upload dua hal: tar.gz lengkap dan JSON ringkas.

```python
def backup_r2(m: Memoria):
    # 1. Buat local tar.gz
    local = m.backup.local()
    archive_path = Path(local["path"])
    # 2. Upload tar.gz
    ok1, msg1 = m.cloud.r2_upload(
        f"snapshots/{archive_path.name}",
        archive_path.read_bytes(),
        "application/gzip",
    )
    # 3. Upload JSON ringkas (untuk restore cepat)
    payload = m.store.export_all()
    payload_bytes = json.dumps(payload, default=str).encode()
    ts = time.strftime("%Y%m%d_%H%M%S")
    ok2, msg2 = m.cloud.r2_upload(
        f"json/{ts}.json", payload_bytes, "application/json",
    )
    return {"ok": ok1 and ok2, "tar": msg1, "json": msg2}
```

### 7.5 Restore dari R2

```python
def restore_r2(m: Memoria, mode="merge"):
    # List JSON snapshots, ambil yang terbaru
    ok, keys = m.cloud.r2_list(prefix="json/")
    if not ok:
        return {"ok": False, "error": keys}
    jsons = sorted([k for k in keys if k.endswith(".json")])
    if not jsons:
        return {"ok": False, "error": "no snapshots found"}
    # Download + import
    ok, data = m.cloud.r2_download(jsons[-1])
    if not ok:
        return {"ok": False, "error": data}
    payload = json.loads(data)
    n = m.store.import_all(payload, mode=mode)
    return {"ok": True, "imported": n, "key": jsons[-1]}
```

---

## 8. Explanation Logger

Setiap keputusan penting agent seharusnya punya jejak audit.
Minimal field yang dicatat:

| Field       | Tipe    | Tujuan                                |
|-------------|---------|---------------------------------------|
| topic       | string  | kategori keputusan                    |
| decision    | string  | apa yang diputuskan                   |
| rationale   | string  | kenapa keputusan itu dibuat           |
| risk        | string  | apa yang bisa salah                   |
| reward      | string  | apa yang bisa benar                   |
| confidence  | float   | 0.0 - 1.0, seberapa yakin             |
| source      | string  | siapa/apa yang membuat keputusan     |
| outcome     | string  | "success"/"partial"/"fail", diisi nanti |

### 8.1 Schema

```sql
CREATE TABLE explanations (
    id           TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    decision     TEXT NOT NULL,
    rationale    TEXT NOT NULL,
    risk         TEXT DEFAULT '',
    reward       TEXT DEFAULT '',
    confidence   REAL DEFAULT 0.5,
    source       TEXT DEFAULT 'agent',
    outcome      TEXT DEFAULT '',
    created_at   INTEGER NOT NULL
);

CREATE INDEX idx_explanations_topic ON explanations(topic);
CREATE INDEX idx_explanations_created ON explanations(created_at DESC);
```

### 8.2 Class Explanations

```python
class Explanations:
    def __init__(self, store):
        self.store = store
        self.markdown_dir = Path("data/explanations")
        self.markdown_dir.mkdir(parents=True, exist_ok=True)

    def log(self, topic, decision, rationale,
            risk="", reward="", confidence=0.5, source="agent"):
        exp_id = uuid.uuid4().hex
        ts = int(time.time() * 1000)
        # Tulis ke SQLite
        self.store.add_explanation(
            topic=topic, decision=decision, rationale=rationale,
            risk=risk, reward=reward, confidence=confidence,
            source=source,
        )
        # Tulis mirror markdown untuk human reading
        self._write_markdown(exp_id, topic, decision, rationale,
                             risk, reward, confidence, ts, source)
        return exp_id

    def _write_markdown(self, exp_id, topic, decision, rationale,
                        risk, reward, confidence, ts, source):
        slug = "".join(c if c.isalnum() else "-"
                      for c in topic.lower())[:50]
        path = self.markdown_dir / f"{exp_id[:12]}-{slug}.md"
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts/1000))
        path.write_text(f"""# {topic}

- **id:** `{exp_id}`
- **timestamp:** {ts_str}
- **source:** {source}
- **confidence:** {confidence:.0%}

## Decision
{decision}

## Rationale
{rationale}

## Risk
{risk or "_not recorded_"}

## Reward
{reward or "_not recorded_"}

---
_Edit `outcome` in SQLite to close the feedback loop._
""")

    def record_outcome(self, exp_id, outcome):
        """Tutup feedback loop: success/partial/fail."""
        return self.store.update_explanation_outcome(exp_id, outcome)
```

### 8.3 Contoh penggunaan

```python
m = Memoria()
eid = m.explain(
    topic="deployment strategy",
    decision="rolling update with health checks",
    rationale="zero downtime, easy rollback, blue/green-like safety",
    risk="briefly serves mixed versions, longer deploy window",
    reward="no maintenance window needed, automatic rollback on bad health",
    confidence=0.85,
)
# ... beberapa hari kemudian ...
m.record_outcome(eid, "success")
```

### 8.4 Search explanations

Karena tabel `explanations` punya FTS5, Anda bisa search:

```python
# Temukan semua keputusan terkait deployment
m.search_explanations("deployment OR rolling")
```

---

## 9. Performance Metrics

Tanpa metrics, Anda tidak tahu apakah sistem Anda sebenarnya
cepat. Modul `metrics` mencatat latency per layer dan hit-rate
cache.

### 9.1 Schema

```sql
CREATE TABLE metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    value      REAL NOT NULL,
    layer      TEXT DEFAULT 'all',
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_metrics_name ON metrics(name, created_at DESC);
```

### 9.2 Class Metrics

```python
from collections import deque
import time

class Metrics:
    def __init__(self, store):
        self.store = store
        # In-memory rolling windows
        self._hit_window = deque(maxlen=1000)
        self._latency_window = deque(maxlen=1000)

    def record_cache(self, hit, latency_ms):
        self._hit_window.append(hit)
        self._latency_window.append(latency_ms)
        self.store.record_metric(
            "cache_hit", 1.0 if hit else 0.0, "redis"
        )
        self.store.record_metric("cache_latency_ms", latency_ms, "redis")

    def record_layer(self, layer, action, latency_ms, success):
        self.store.record_metric(f"{layer}_{action}_ms", latency_ms, layer)
        self.store.record_metric(
            f"{layer}_{action}_ok", 1.0 if success else 0.0, layer
        )

    def summary(self):
        if not self._hit_window:
            cache = {"hit_rate": 0.0, "samples": 0}
        else:
            cache = {
                "hit_rate": sum(self._hit_window) / len(self._hit_window),
                "samples": len(self._hit_window),
            }
        if not self._latency_window:
            lat = {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "samples": 0}
        else:
            sorted_lat = sorted(self._latency_window)
            n = len(sorted_lat)
            lat = {
                "p50_ms": sorted_lat[int(n*0.5) - 1],
                "p95_ms": sorted_lat[int(n*0.95) - 1],
                "p99_ms": sorted_lat[int(n*0.99) - 1],
                "samples": n,
            }
        return {"cache": cache, "latency": lat}
```

### 9.3 Context manager untuk auto-timing

```python
import time

class TimerCM:
    def __init__(self, metrics, layer, action):
        self.metrics = metrics
        self.layer = layer
        self.action = action
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self
    def __exit__(self, *exc):
        latency = (time.perf_counter() - self.t0) * 1000
        success = not exc[0] if exc else True
        self.metrics.record_layer(self.layer, self.action, latency, success)
        return False  # jangan swallow exception

# Penggunaan
with TimerCM(metrics, "sqlite", "read"):
    row = store.get(key)
```

### 9.4 Dashboard

```python
print(m.stats())
# {
#   "store": {"memories": 1234, "explanations": 87, "db_size_bytes": 245678},
#   "performance": {
#     "cache": {"hit_rate": 0.87, "samples": 940},
#     "latency": {"p50_ms": 0.4, "p95_ms": 2.1, "p99_ms": 4.5, "samples": 940}
#   }
# }
```

---

## 10. Backup & Restore

### 10.1 Backup lokal (selalu bekerja)

```python
import tarfile, json
from pathlib import Path
import time

class Backup:
    def __init__(self, store, backup_dir="data/backups"):
        self.store = store
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def local(self):
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = self.backup_dir / f"memoria-{ts}.tar.gz"
        payload = self.store.export_all()
        manifest = {
            "created_at": int(time.time() * 1000),
            "version": 1,
            "counts": {
                "memories": len(payload["memories"]),
                "explanations": len(payload["explanations"]),
            },
        }
        with tarfile.open(out, "w:gz") as tar:
            # Manifest
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
            # Data
            data_bytes = json.dumps(payload, default=str).encode()
            info = tarfile.TarInfo("data.json")
            info.size = len(data_bytes)
            tar.addfile(info, io.BytesIO(data_bytes))
            # Live sqlite sebagai belt-and-braces
            db_path = Path(self.store.db_path)
            if db_path.exists():
                tar.add(str(db_path), arcname=f"live/{db_path.name}")
        return {"path": str(out), "size_bytes": out.stat().st_size,
                "manifest": manifest}
```

### 10.2 Backup ke Supabase + R2

```python
def backup_all(m):
    out = {"local": m.backup.local(), "supabase": None, "r2": None}
    try:
        out["supabase"] = m.backup.supabase()
    except Exception as e:
        out["supabase"] = {"ok": False, "error": str(e)}
    try:
        out["r2"] = m.backup.r2()
    except Exception as e:
        out["r2"] = {"ok": False, "error": str(e)}
    return out
```

### 10.3 Restore

```python
class Restore:
    def local(self, archive_path, mode="merge"):
        """mode=merge (upsert) atau mode=replace (wipe+reimport)."""
        with tarfile.open(archive_path, "r:gz") as tar:
            members = {m.name: m for m in tar.getmembers()}
            f = tar.extractfile(members["data.json"])
            payload = json.loads(f.read().decode("utf-8"))
        n = self.store.import_all(payload, mode=mode)
        return {"ok": True, "imported": n}

    def latest_local(self, mode="merge"):
        """Restore dari tar.gz paling baru."""
        archives = sorted(self.backup_dir.glob("memoria-*.tar.gz"),
                          reverse=True)
        if not archives:
            return {"ok": False, "error": "no backups"}
        return self.local(archives[0], mode=mode)
```

### 10.4 Format backup

Setiap tar.gz backup berisi:

```
memoria-20260621_120000.tar.gz
├── manifest.json          # metadata: kapan, berapa rows, versi
├── data.json               # semua memories + explanations
└── live/
    └── memoria.db          # copy of live sqlite file
```

`data.json` adalah JSON murni yang bisa dibaca dengan `jq`
tanpa perlu software khusus. Ini memastikan backup bisa
di-restore bahkan di environment tanpa Memoria terinstall.

---

## 11. CLI

CLI membuat semua fungsi bisa diakses dari terminal tanpa
menulis Python. Pakai `argparse` standar.

```python
#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoria import Memoria

def main():
    p = argparse.ArgumentParser(prog="memoria")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="health check")
    sub.add_parser("stats", help="performance dashboard")

    s = sub.add_parser("set", help="store a memory")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--category", default="general")
    s.add_argument("--tag", action="append", default=[])

    g = sub.add_parser("get", help="retrieve")
    g.add_argument("key")

    sr = sub.add_parser("search", help="FTS5 search")
    sr.add_argument("query")
    sr.add_argument("--limit", type=int, default=10)

    e = sub.add_parser("explain", help="log decision")
    e.add_argument("topic")
    e.add_argument("decision")
    e.add_argument("--rationale", required=True)
    e.add_argument("--risk", default="")
    e.add_argument("--reward", default="")
    e.add_argument("--confidence", type=float, default=0.5)

    b = sub.add_parser("backup")
    b.add_argument("target", nargs="?", default="local",
                   choices=["local", "supabase", "r2", "all"])

    rs = sub.add_parser("restore")
    rs.add_argument("source",
                    help="local <path> | local:latest | supabase | r2")
    rs.add_argument("--mode", choices=["merge", "replace"], default="merge")

    args = p.parse_args()
    m = Memoria()
    # dispatch ke method yang sesuai...
```

### 11.1 Contoh penggunaan CLI

```bash
# Health check
./bin/memoria doctor

# Tulis memory
./bin/memoria set user:pref:theme '"dark"' --category ui

# Cari
./bin/memoria search "theme"

# Log keputusan
./bin/memoria explain \
    "deployment strategy" \
    "rolling update" \
    --rationale "zero downtime" \
    --risk "rollback complexity" \
    --reward "no maintenance window" \
    --confidence 0.85

# Backup
./bin/memoria backup           # local
./bin/memoria backup all       # semua layer yang aktif

# Restore
./bin/memoria restore local:latest
./bin/memoria restore supabase
./bin/memoria restore r2

# Dashboard
./bin/memoria stats
```

### 11.2 Shell REPL

Untuk eksplorasi interaktif:

```bash
$ ./bin/memoria shell
Memoria REPL. Type 'help' or 'quit'.
memoria> doctor
{...json...}
memoria> list
user:pref:theme
agent:strategy:default
memoria> search theme
[...results...]
memoria> backup
{...json...}
memoria> quit
```

---

## 12. Cron Scheduling

Agent production butuh backup terjadwal. Cron Linux standar cukup.

### 12.1 Install backup harian

```python
# scripts/install_cron.py
import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
CRON_LINE = (
    "15 3 * * * "  # jam 03:15 setiap hari
    f"cd {ROOT} && {PYTHON} -m memoria.bin.memoria backup all "
    f">> {ROOT}/data/backup.log 2>&1"
)

def _read_cron():
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""

def _write_cron(content):
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)

def main():
    current = _read_cron()
    lines = [l for l in current.splitlines() if "memoria" not in l]
    lines.append(CRON_LINE)
    _write_cron("\n".join(lines) + "\n")
    print(f"Installed: {CRON_LINE}")

if __name__ == "__main__":
    main()
```

```bash
python3 scripts/install_cron.py
crontab -l | grep memoria
# 15 3 * * * cd /path/to/memoria && python3 -m memoria.bin.memoria backup all >> ...
```

### 12.2 Cleanup backup lama

```bash
#!/usr/bin/env bash
# scripts/cleanup.sh — hapus backup >30 hari
DAYS="${1:-30}"
BACKUP_DIR="${MEMORIA_BACKUP_DIR:-$HOME/memoria/data/backups}"
find "$BACKUP_DIR" -name 'memoria-*.tar.gz' -mtime "+$DAYS" -delete
```

```bash
chmod +x scripts/cleanup.sh
# Tambahkan ke crontab setelah backup utama:
# 30 3 * * *  /path/to/memoria/scripts/cleanup.sh
```

### 12.3 Monitor backup

Tambahkan ke crontab:

```cron
# Kirim alert ke Discord jika backup gagal (opsional)
15 4 * * *  grep -i "error" /path/to/memoria/data/backup.log | tail -5 | curl -X POST -H "Content-Type: application/json" -d @- https://your-webhook-url
```

---

## 13. Testing

Test yang baik harus bisa dijalankan tanpa setup cloud apapun.
Pattern: temp directory per test, fresh config singleton, cleanup
di akhir.

### 13.1 Setup test config

```python
import tempfile, shutil
from pathlib import Path
import memoria.config as cfg_mod

def _make_tmp():
    tmp = Path(tempfile.mkdtemp(prefix="memoria_test_"))
    (tmp / "data" / "backups").mkdir(parents=True)
    (tmp / "data" / "explanations").mkdir(parents=True)
    (tmp / "schema.sql").symlink_to(Path("schema.sql").resolve())
    (tmp / "memoria").symlink_to(Path("memoria").resolve())
    cfg_mod._singleton = None
    c = cfg_mod.get_config()
    c.sqlite_path = str(tmp / "data" / "memoria.db")
    c.backup_dir = str(tmp / "data" / "backups")
    c.explanations_dir = str(tmp / "data" / "explanations")
    c.redis_enabled = False
    c.supabase_enabled = False
    c.r2_enabled = False
    return tmp
```

### 13.2 Test cases esensial

```python
def test_store_roundtrip():
    tmp = _make_tmp()
    from memoria import Store
    s = Store()
    mid = s.put("k", {"v": 1})
    row = s.get("k")
    assert row["value"] == {"v": 1}
    assert row["access_count"] >= 1
    shutil.rmtree(tmp, ignore_errors=True)

def test_search():
    tmp = _make_tmp()
    from memoria import Store
    s = Store()
    s.put("a", "hello world")
    s.put("b", "goodbye world")
    results = s.search("hello")
    assert len(results) == 1
    assert results[0]["key"] == "a"

def test_backup_restore():
    tmp = _make_tmp()
    from memoria import Store, Backup, Restore
    s = Store()
    s.put("alpha", 1)
    s.put("beta", 2)
    archive = Backup(s).local()
    # Wipe
    with s._conn() as c:
        c.execute("DELETE FROM memories")
    assert s.count() == 0
    # Restore
    n = Restore(s).local(archive["path"], mode="replace")["imported"]
    assert s.count() == 2
    shutil.rmtree(tmp, ignore_errors=True)

def test_explanations():
    tmp = _make_tmp()
    from memoria import Store, Explanations
    s = Store()
    e = Explanations(s)
    eid = e.log("topic", "decision", "rationale",
                risk="r", reward="w", confidence=0.7)
    items = e.list(topic="topic")
    assert len(items) == 1
    assert items[0]["id"] == eid
    shutil.rmtree(tmp, ignore_errors=True)

def test_cache_graceful_degradation():
    from memoria import Cache
    c = Cache(enabled=True, host="invalid-host")
    assert c.is_connected is False
    assert c.get("k") is None
    assert c.put("k", "v") is False
    # No exceptions raised
```

### 13.3 Run tests

```bash
cd /path/to/memoria
python3 tests/test_memoria.py

# Expected:
# ========================================
# 8 passed, 0 failed
```

Atau dengan pytest:

```bash
pip install pytest
pytest tests/ -v
```

---

## 14. Production Checklist

Sebelum go-live, verifikasi semua poin ini.

### Keamanan

- [ ] `.env` permission: `chmod 600 .env`
- [ ] `.env` tidak masuk git (cek `.gitignore`)
- [ ] Supabase: pakai **service_role** key, simpan di `.env` only
- [ ] R2: token di-scope ke satu bucket
- [ ] Cron logs: redact secrets, simpan di `data/` (bukan `/var/log`)
- [ ] Backup tarball: tidak ada plaintext password di `value` JSON

### Monitoring

- [ ] `memoria doctor` di-run tiap 6 jam via cron
- [ ] Alert jika ada layer `status: error` di output
- [ ] `memoria stats` di-archive mingguan untuk trend analysis
- [ ] Disk usage pada `data/` di-monitor (alert jika >1GB)

### Backup verification

- [ ] Test restore dari local archive: `./bin/memoria restore local:latest`
- [ ] Test restore dari Supabase: `./bin/memoria restore supabase`
- [ ] Test restore dari R2: `./bin/memoria restore r2`
- [ ] Verifikasi count rows sebelum & sesudah restore
- [ ] Cron backup berjalan tiap hari (cek `data/backup.log`)

### Disaster recovery drill

- [ ] Simulasi VPS hilang: spin up VPS baru, jalankan
  `git clone ... && ./bin/memoria restore r2`
- [ ] Pastikan semua memories + explanations kembali
- [ ] Pastikan struktur schema identik (cek `doctor` output)

### Performa

- [ ] `cache_hit_rate > 0.5` (jika Redis aktif)
- [ ] `p95_latency < 50ms` untuk `get()` lokal
- [ ] `p95_latency < 2s` untuk backup ke cloud
- [ ] DB size < 100MB (di atas itu, pertimbangkan archive ke R2)

---

## 15. Troubleshooting

### Doctor: sqlite status = error

**Gejala:** `doctor.sqlite.status == "error"`

**Penyebab umum:**
- File permission: `chmod 644 data/memoria.db data/` (parent writable)
- Disk full: cek `df -h`
- Corrupt DB: jalankan `sqlite3 data/memoria.db 'PRAGMA integrity_check;'`

### Doctor: redis status = error

**Gejala:** `doctor.redis.status == "error"` meskipun `redis-cli ping` return PONG

**Penyebab umum:**
- `REDIS_ENABLED=false` di `.env` — pastikan true
- Password salah — cek `redis-cli -a YOUR_PASS ping`
- Untuk Upstash: `REDIS_URL` tidak lengkap, atau ada karakter
  yang perlu di-URL-encode
- Firewall VPS: cek `sudo ufw status`

### Doctor: supabase status = error / table not found

**Gejala:** `doctor.supabase.status == "error"` dengan error
`table 'memories' not found`

**Solusi:**
1. Buka Supabase dashboard → SQL Editor
2. Copy paste seluruh isi `schema.sql`
3. Klik **Run** dan tunggu selesai
4. Run `memoria doctor` lagi

### Doctor: r2 status = error

**Gejala:** `doctor.r2.status == "error"`

**Solusi:**
1. Cek R2 token belum expired: Dashboard → R2 → API Tokens
2. Pastikan bucket exists: Dashboard → R2 → Buckets
3. Test dengan wrangler:
   ```bash
   wrangler r2 object put test-bucket/test.txt --content "hello"
   ```
4. Pastikan `R2_ACCOUNT_ID` benar (bukan zone ID)

### Backup file corrupt

**Gejala:** `restore.local()` return `ok=false` dengan error
`tarfile.ReadError`

**Solusi:**
1. List isi tar.gz: `tar -tzf data/backups/memoria-XXX.tar.gz | head`
2. Jika kosong, disk penuh saat backup — free space, retry
3. Jika `data.json` missing, archive corrupt — pakai backup
   sebelumnya atau restore dari Supabase/R2

### Cache selalu miss (hit_rate = 0)

**Gejala:** `stats.performance.cache.hit_rate == 0` padahal
Redis aktif

**Penyebab:**
- TTL terlalu pendek: naikkan `REDIS_TTL_HOT` di `.env`
- Key tidak konsisten: `m.set("Foo")` dan `m.get("foo")` adalah
  key berbeda (SQLite case-sensitive)
- Restart Redis baru: cache kosong, normal setelah warm-up

### Schema mismatch antara local dan Supabase

**Gejala:** Restore dari Supabase skip banyak rows, atau error
saat upsert

**Solusi:**
1. Compare `schema.sql` lokal dengan yang di-paste ke Supabase
2. Jika ada kolom baru, tambahkan manual di Supabase:
   ```sql
   ALTER TABLE memories ADD COLUMN new_field TEXT DEFAULT '';
   ```
3. Re-run backup

### Restore stuck di mode="replace"

**Gejala:** `restore.local(... mode="replace")` return ok tapi
data tidak berubah

**Penyebab:** Transaction tidak commit. Solusi: pastikan
`isolation_level=None` (autocommit) di `_conn()` context manager.

---

## Lampiran: Referensi Lanjutan

- **SQLite FTS5**: https://www.sqlite.org/fts5.html
- **Redis Best Practices**: https://redis.io/docs/manual/
- **Supabase REST API**: https://supabase.com/docs/guides/api
- **Cloudflare R2 S3 API**: https://developers.cloudflare.com/r2/api/s3/api/
- **Python `tarfile`**: https://docs.python.org/3/library/tarfile.html
- **Python `argparse`**: https://docs.python.org/3/library/argparse.html

## Lisensi

Tutorial ini dirilis di bawah MIT License. Silakan gunakan,
modifikasi, dan distribusikan kembali dengan menyertakan
attribution.

## Kontribusi

Kontribusi diterima via pull request. Untuk perubahan besar,
buka issue dulu untuk diskusi.