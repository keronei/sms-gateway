"""
schema.py - tables for the contributions feature.

Normalization notes:
  - senders, regions, paybills, accounts, targets and contributions are
    each their own table; contributions only hold foreign keys.
  - contributions.region_id is a deliberate, documented snapshot: it's set
    when a contribution is attributed (back-filled when a sender first gets
    a region) so that a sender later moving wards does not rewrite history.
    senders.region_id is always the sender's CURRENT region.
  - money is stored as integer cents; times as naive local-time strings
    'YYYY-MM-DD HH:MM:SS' exactly as printed in the SMS (the Pi's own clock
    can't be trusted offline, so the payment's own timestamp is the source
    of truth for every date filter).
"""
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS contrib_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER REFERENCES regions(id),
    kind       TEXT NOT NULL DEFAULT 'ward',          -- constituency, ward, ...
    name       TEXT NOT NULL,
    code       TEXT NOT NULL COLLATE NOCASE UNIQUE,   -- short code senders can text
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regions_parent ON regions(parent_id);

CREATE TABLE IF NOT EXISTS region_aliases (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL COLLATE NOCASE,
    UNIQUE(alias)
);

CREATE TABLE IF NOT EXISTS paybills (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     TEXT NOT NULL UNIQUE,
    name       TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    paybill_id   INTEGER NOT NULL REFERENCES paybills(id),
    account_ref  TEXT NOT NULL,       -- exactly as it appears in the SMS (masked)
    label        TEXT,                -- human name, e.g. the project
    active       INTEGER NOT NULL DEFAULT 1,
    auto_created INTEGER NOT NULL DEFAULT 0,   -- 1 = first seen in a message, not yet reviewed
    created_at   REAL NOT NULL,
    UNIQUE(paybill_id, account_ref)
);

CREATE TABLE IF NOT EXISTS targets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id    INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    starts_on    TEXT,                -- 'YYYY-MM-DD', NULL = open-ended
    ends_on      TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_targets_lookup ON targets(region_id, account_id);

CREATE TABLE IF NOT EXISTS senders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    phone          TEXT NOT NULL UNIQUE,
    name           TEXT,
    region_id      INTEGER REFERENCES regions(id),
    region_set_at  REAL,
    region_set_by  TEXT,              -- 'sms' | 'dashboard'
    region_prompts INTEGER NOT NULL DEFAULT 0,
    blocked        INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_senders_region ON senders(region_id);

-- One row per inbound message ever looked at. UNIQUE(source, source_ref) is
-- what makes ingestion idempotent (and is the seam a WhatsApp source plugs
-- into later: source='whatsapp', source_ref=<provider message id>).
CREATE TABLE IF NOT EXISTS ingest_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    sender_id  INTEGER REFERENCES senders(id),
    phone_raw  TEXT,
    body       TEXT,
    received_at REAL,
    outcome    TEXT NOT NULL DEFAULT 'processing',
        -- recorded, duplicate, region_set, region_unknown, rejected,
        -- wrong_paybill, unparseable, ignored, blocked, error
    detail     TEXT,
    review     TEXT,                  -- NULL | 'open' | 'dismissed'
    created_at REAL NOT NULL,
    UNIQUE(source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_ingest_review ON ingest_log(review);

CREATE TABLE IF NOT EXISTS contributions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_code     TEXT NOT NULL UNIQUE,          -- M-PESA ref: the double-entry guard
    sender_id    INTEGER NOT NULL REFERENCES senders(id),
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    region_id    INTEGER REFERENCES regions(id),   -- snapshot, see module docstring
    amount_cents INTEGER NOT NULL,
    paid_at      TEXT NOT NULL,                 -- 'YYYY-MM-DD HH:MM:SS' local, from the SMS
    ingest_id    INTEGER REFERENCES ingest_log(id),
    status       TEXT NOT NULL DEFAULT 'valid', -- valid | void
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contrib_paid   ON contributions(paid_at);
CREATE INDEX IF NOT EXISTS idx_contrib_region ON contributions(region_id, account_id);
CREATE INDEX IF NOT EXISTS idx_contrib_sender ON contributions(sender_id);

CREATE TABLE IF NOT EXISTS duplicate_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contribution_id INTEGER NOT NULL REFERENCES contributions(id) ON DELETE CASCADE,
    sender_id       INTEGER REFERENCES senders(id),
    ingest_id       INTEGER REFERENCES ingest_log(id),
    created_at      REAL NOT NULL
);

-- Replies are decided inside the ingest transaction but SENT later by a
-- separate loop (outbox pattern), so ingesting never waits on the modem and
-- a crash can't lose or double-send a decided reply.
CREATE TABLE IF NOT EXISTS outbound_replies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id       INTEGER REFERENCES senders(id),
    phone           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued, submitted, sent, failed, expired
    command_id      INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      REAL NOT NULL,
    next_attempt_at REAL,
    submitted_at    REAL,
    sent_at         REAL
);
CREATE INDEX IF NOT EXISTS idx_replies_status ON outbound_replies(status);
CREATE INDEX IF NOT EXISTS idx_replies_sender ON outbound_replies(sender_id, created_at);

-- data_version is bumped on every change that affects what the sheet shows;
-- the sync only runs (and only goes online) when data_version > synced_version.
CREATE TABLE IF NOT EXISTS sheet_sync_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    data_version    INTEGER NOT NULL DEFAULT 1,
    synced_version  INTEGER NOT NULL DEFAULT 0,
    last_attempt_at REAL,
    last_success_at REAL,
    last_error      TEXT,
    last_result     TEXT
);
"""


def init(conn):
    """Create tables and seed once. Idempotent; called from db.init_db()."""
    conn.executescript(SCHEMA)
    seeded = conn.execute("SELECT value FROM contrib_settings WHERE key = 'seeded'").fetchone()
    if not seeded:
        # seeded exactly once so deleting the default paybill later sticks
        conn.execute(
            "INSERT OR IGNORE INTO paybills (number, name, created_at) VALUES ('522522', 'KCB Pay Bill', ?)",
            (time.time(),),
        )
        conn.execute("INSERT OR REPLACE INTO contrib_settings (key, value) VALUES ('seeded', '1')")
    conn.execute("INSERT OR IGNORE INTO sheet_sync_state (id) VALUES (1)")
