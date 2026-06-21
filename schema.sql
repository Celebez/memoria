-- Memoria canonical schema (SQLite, mirrored to Supabase)
-- Versioned: schema_version row is checked on every open.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,          -- uuid hex
    key          TEXT UNIQUE NOT NULL,       -- user-defined lookup key
    value        TEXT NOT NULL,              -- JSON-encoded payload
    category     TEXT DEFAULT 'general',
    tags         TEXT DEFAULT '',            -- comma-separated
    source       TEXT DEFAULT 'local',       -- local|redis|supabase|r2
    access_count INTEGER DEFAULT 0,
    created_at   INTEGER NOT NULL,           -- unix epoch ms
    updated_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_updated  ON memories(updated_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    key, value, category, tags,
    content='memories', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, key, value, category, tags)
    VALUES (new.rowid, new.key, new.value, new.category, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, value, category, tags)
    VALUES ('delete', old.rowid, old.key, old.value, old.category, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, value, category, tags)
    VALUES ('delete', old.rowid, old.key, old.value, old.category, old.tags);
    INSERT INTO memories_fts(rowid, key, value, category, tags)
    VALUES (new.rowid, new.key, new.value, new.category, new.tags);
END;

CREATE TABLE IF NOT EXISTS explanations (
    id           TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    decision     TEXT NOT NULL,
    rationale    TEXT NOT NULL,
    risk         TEXT DEFAULT '',
    reward       TEXT DEFAULT '',
    confidence   REAL DEFAULT 0.5,           -- 0.0 - 1.0
    source       TEXT DEFAULT 'agent',
    outcome      TEXT DEFAULT '',            -- filled later: success|partial|fail
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_explanations_topic     ON explanations(topic);
CREATE INDEX IF NOT EXISTS idx_explanations_created   ON explanations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_explanations_confidence ON explanations(confidence);

CREATE VIRTUAL TABLE IF NOT EXISTS explanations_fts USING fts5(
    topic, decision, rationale, risk, reward, outcome,
    content='explanations', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS explanations_ai AFTER INSERT ON explanations BEGIN
    INSERT INTO explanations_fts(rowid, topic, decision, rationale, risk, reward, outcome)
    VALUES (new.rowid, new.topic, new.decision, new.rationale, new.risk, new.reward, new.outcome);
END;

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    layer      TEXT NOT NULL,                -- sqlite|redis|supabase|r2
    action     TEXT NOT NULL,                -- read|write|delete|backup|restore
    target     TEXT,                         -- key or table name
    status     TEXT NOT NULL,                -- ok|error|skip
    latency_ms REAL,
    error      TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_layer   ON audit_log(layer);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,                -- cache_hit_rate, p99_latency, ...
    value      REAL NOT NULL,
    layer      TEXT DEFAULT 'all',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name, created_at DESC);

INSERT OR IGNORE INTO schema_meta(key, value) VALUES
    ('schema_version', '1'),
    ('created_at', strftime('%s','now') * 1000);