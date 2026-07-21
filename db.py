"""
db.py - tiny SQLite data layer, no ORM.

Tables:
  settings     single-row config for the SMS gateway connection + sending behaviour
  campaigns    one row per "mail merge run" (template + csv + mapping)
  recipients   one row per recipient/message inside a campaign
"""
import sqlite3
import json
import os
import time
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dashboard.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")     # two separate processes (Flask + modem
    conn.execute("PRAGMA busy_timeout = 5000")    # daemon) write to this file concurrently
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                address TEXT DEFAULT '',
                port INTEGER DEFAULT 8080,
                use_https INTEGER DEFAULT 0,
                username TEXT DEFAULT '',
                password TEXT DEFAULT '',
                default_country_code TEXT DEFAULT '',
                sim_number INTEGER,
                with_delivery_report INTEGER DEFAULT 1,
                delay_seconds REAL DEFAULT 2.0,
                batch_size INTEGER DEFAULT 20,
                batch_pause_seconds REAL DEFAULT 15.0,

                -- which backend actually sends SMS; 'modem' wired up in a later milestone
                sms_backend TEXT DEFAULT 'android_gateway',

                -- modem hardware / PPP configuration
                modem_control_port TEXT DEFAULT '/dev/ttyUSB0',
                modem_data_port TEXT DEFAULT '/dev/ttyUSB1',
                modem_baud INTEGER DEFAULT 115200,
                modem_apn TEXT DEFAULT '',
                modem_ppp_username TEXT DEFAULT '',
                modem_ppp_password TEXT DEFAULT '',
                modem_sim_pin TEXT DEFAULT '',
                modem_gpio_power_pin INTEGER DEFAULT 17,
                modem_auto_connect INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                template_text TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                phone_column TEXT,
                mapping_json TEXT,
                csv_filename TEXT,
                status TEXT DEFAULT 'draft',   -- draft, ready, dispatching, paused, completed, stopped
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                row_index INTEGER NOT NULL,
                phone_raw TEXT,
                phone_normalized TEXT,
                data_json TEXT NOT NULL,
                filled_message TEXT NOT NULL,
                char_count INTEGER,
                segment_count INTEGER,
                status TEXT DEFAULT 'pending', -- pending, invalid, queued, sending, sent, failed, skipped
                gateway_message_id TEXT,
                error TEXT,
                sent_at REAL,
                updated_at REAL
            );

            CREATE INDEX IF NOT EXISTS idx_recipients_campaign ON recipients(campaign_id);

            -- ============================================================ modem
            -- Owned/written by the modem-manager daemon (modem/manager.py); read by
            -- Flask. Flask never touches the serial ports or GPIO directly — it only
            -- reads these tables and drops rows into modem_commands.

            CREATE TABLE IF NOT EXISTS modem_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                device_present INTEGER DEFAULT 0,
                control_port TEXT,
                data_port TEXT,
                at_ready INTEGER DEFAULT 0,
                sim_status TEXT DEFAULT 'unknown',      -- unknown, ready, pin_required, error, absent
                signal_quality INTEGER,                 -- raw AT+CSQ rssi, 0-31, 99=unknown
                network_reg_status TEXT DEFAULT 'unknown',
                operator TEXT,
                ppp_state TEXT DEFAULT 'down',           -- down, dialing, connected, backoff
                ppp_ip TEXT,
                ppp_last_error TEXT,
                ppp_retry_count INTEGER DEFAULT 0,
                ppp_next_retry_at REAL,
                ppp_connected_since REAL,
                power_cycle_count INTEGER DEFAULT 0,
                last_updated REAL,

                ussd_active INTEGER DEFAULT 0,        -- true while a session awaits our reply
                ussd_last_message TEXT,                -- last decoded text from the network
                ussd_last_state INTEGER,               -- last <m> code (0=done,1=reply needed,2/4/5=error)
                ussd_updated_at REAL,

                last_caller TEXT,                      -- most recent incoming call's number (via +CLIP)
                last_call_at REAL
            );

            CREATE TABLE IF NOT EXISTS modem_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_number TEXT,           -- NULL until +CLIP arrives, or stays NULL if withheld
                ring_count INTEGER DEFAULT 1,
                first_ring_at REAL NOT NULL,
                last_ring_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_modem_calls_first_ring ON modem_calls(first_ring_at);

            CREATE TABLE IF NOT EXISTS modem_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                level TEXT NOT NULL,          -- info, warn, error
                category TEXT NOT NULL,       -- power, port, at, ppp, urc, sms, ussd, system
                message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_modem_events_ts ON modem_events(ts);

            -- Flask -> daemon action requests (power-cycle, reconnect, and later
            -- send-sms/send-ussd/etc). The daemon polls this table frequently.
            CREATE TABLE IF NOT EXISTS modem_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,        -- power_cycle, reconnect_ppp, send_sms, ...
                payload_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending', -- pending, done, failed
                result TEXT,
                created_at REAL NOT NULL,
                completed_at REAL
            );

            -- Received SMS, copied off the SIM (which only holds ~40 messages) as
            -- soon as the daemon sees them, then deleted from SIM storage. This
            -- table is the durable inbox from then on.
            CREATE TABLE IF NOT EXISTS modem_inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT,
                body TEXT,
                raw_timestamp TEXT,
                received_at REAL,
                sim_index INTEGER,
                status TEXT DEFAULT 'unread',  -- unread, read
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_modem_inbox_created ON modem_inbox(created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (id) VALUES (1)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO modem_status (id, last_updated) VALUES (1, ?)", (time.time(),)
        )


# ---------------------------------------------------------------- settings
def get_settings():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return dict(row) if row else {}


def save_settings(data: dict):
    fields = [
        "address", "port", "use_https", "username", "password",
        "default_country_code", "sim_number", "with_delivery_report",
        "delay_seconds", "batch_size", "batch_pause_seconds",
        "sms_backend",
        "modem_control_port", "modem_data_port", "modem_baud", "modem_apn",
        "modem_ppp_username", "modem_ppp_password", "modem_sim_pin",
        "modem_gpio_power_pin", "modem_auto_connect",
    ]
    updates = {k: data[k] for k in fields if k in data}
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = 1
    with get_conn() as conn:
        conn.execute(f"UPDATE settings SET {set_clause} WHERE id = :id", updates)
    return get_settings()


# ---------------------------------------------------------------- templates library
def list_templates():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM templates ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def save_template(name, body):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO templates (name, body, created_at) VALUES (?, ?, ?)",
            (name, body, time.time()),
        )
        return cur.lastrowid


def delete_template(template_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


# ---------------------------------------------------------------- campaigns
def create_campaign(name, template_text, fields, phone_column, mapping, csv_filename):
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO campaigns
               (name, template_text, fields_json, phone_column, mapping_json, csv_filename,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (name, template_text, json.dumps(fields), phone_column, json.dumps(mapping),
             csv_filename, now, now),
        )
        return cur.lastrowid


def add_recipients(campaign_id, recipients):
    """recipients: list of dicts with keys row_index, phone_raw, phone_normalized,
    data, filled_message, char_count, segment_count, status, error"""
    now = time.time()
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO recipients
               (campaign_id, row_index, phone_raw, phone_normalized, data_json,
                filled_message, char_count, segment_count, status, error, updated_at)
               VALUES (:campaign_id, :row_index, :phone_raw, :phone_normalized, :data_json,
                       :filled_message, :char_count, :segment_count, :status, :error, :updated_at)""",
            [
                {
                    "campaign_id": campaign_id,
                    "row_index": r["row_index"],
                    "phone_raw": r.get("phone_raw"),
                    "phone_normalized": r.get("phone_normalized"),
                    "data_json": json.dumps(r.get("data", {})),
                    "filled_message": r["filled_message"],
                    "char_count": r.get("char_count"),
                    "segment_count": r.get("segment_count"),
                    "status": r.get("status", "pending"),
                    "error": r.get("error"),
                    "updated_at": now,
                }
                for r in recipients
            ],
        )


def set_campaign_status(campaign_id, status):
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), campaign_id),
        )


def get_campaign(campaign_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return dict(row) if row else None


def list_campaigns():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id = c.id) as total,
                      (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id = c.id AND r.status = 'sent') as sent,
                      (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id = c.id AND r.status = 'failed') as failed
               FROM campaigns c ORDER BY c.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_recipients(campaign_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM recipients WHERE campaign_id = ? ORDER BY row_index", (campaign_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recipient(recipient_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM recipients WHERE id = ?", (recipient_id,)).fetchone()
        return dict(row) if row else None


def update_recipient(recipient_id, **fields):
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = recipient_id
    with get_conn() as conn:
        conn.execute(f"UPDATE recipients SET {set_clause} WHERE id = :id", fields)


def campaign_status_counts(campaign_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM recipients WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def delete_campaign(campaign_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))


# ==================================================================== modem
# Written by modem/manager.py (the standalone daemon), read by Flask.
# Flask only ever reads modem_status/modem_events and writes modem_commands —
# it never touches serial ports or GPIO itself.

def get_modem_status():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM modem_status WHERE id = 1").fetchone()
        return dict(row) if row else {}


def update_modem_status(**fields):
    if not fields:
        return
    fields["last_updated"] = time.time()
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE modem_status SET {set_clause} WHERE id = 1", fields)


def add_modem_event(level, category, message):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO modem_events (ts, level, category, message) VALUES (?, ?, ?, ?)",
            (time.time(), level, category, message),
        )
        # keep the log table bounded — trim occasionally rather than on every insert
        row = conn.execute("SELECT COUNT(*) as n FROM modem_events").fetchone()
        if row["n"] > 5000:
            conn.execute(
                """DELETE FROM modem_events WHERE id IN (
                       SELECT id FROM modem_events ORDER BY id ASC LIMIT ?
                   )""",
                (row["n"] - 4000,),
            )


def get_modem_events(limit=200, since_id=None, level=None, category=None):
    query = "SELECT * FROM modem_events WHERE 1=1"
    params = []
    if since_id is not None:
        query += " AND id > ?"
        params.append(since_id)
    if level:
        query += " AND level = ?"
        params.append(level)
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in reversed(rows)]


def enqueue_modem_command(command, payload=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO modem_commands (command, payload_json, created_at) VALUES (?, ?, ?)",
            (command, json.dumps(payload or {}), time.time()),
        )
        return cur.lastrowid


def get_pending_modem_commands():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM modem_commands WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def claim_modem_command(command_id):
    """Marks a command as picked up/in-progress so the poll loop doesn't
    dispatch it a second time on its next pass."""
    with get_conn() as conn:
        conn.execute("UPDATE modem_commands SET status = 'running' WHERE id = ?", (command_id,))


def complete_modem_command(command_id, status, result=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE modem_commands SET status = ?, result = ?, completed_at = ? WHERE id = ?",
            (status, result, time.time(), command_id),
        )


def get_modem_command(command_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM modem_commands WHERE id = ?", (command_id,)).fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------- modem inbox
def add_modem_inbox_message(sender, body, raw_timestamp, received_at, sim_index=None):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO modem_inbox (sender, body, raw_timestamp, received_at, sim_index, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sender, body, raw_timestamp, received_at, sim_index, time.time()),
        )
        return cur.lastrowid


def get_modem_inbox(limit=200):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM modem_inbox ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_modem_inbox_read(inbox_id):
    with get_conn() as conn:
        conn.execute("UPDATE modem_inbox SET status = 'read' WHERE id = ?", (inbox_id,))


def delete_modem_inbox_message(inbox_id):
    # the SIM copy is already gone by the time a message reaches this table -
    # this only ever deletes our durable DB copy.
    with get_conn() as conn:
        conn.execute("DELETE FROM modem_inbox WHERE id = ?", (inbox_id,))


# -------------------------------------------------------------- call log
def log_call_ring(number=None):
    """Starts a new call-log entry (first RING/CLIP of a call not already
    being tracked). Returns the new entry's id."""
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO modem_calls (caller_number, ring_count, first_ring_at, last_ring_at, created_at)
               VALUES (?, 1, ?, ?, ?)""",
            (number, now, now, now),
        )
        return cur.lastrowid


def bump_call_ring(call_id):
    """A repeat RING for a call already being tracked - same call, not a new one."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE modem_calls SET ring_count = ring_count + 1, last_ring_at = ? WHERE id = ?",
            (time.time(), call_id),
        )


def update_call_number(call_id, number):
    """Fills in the caller's number once +CLIP arrives for a call that was
    first logged from a bare RING (number not yet known)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE modem_calls SET caller_number = ?, last_ring_at = ? WHERE id = ?",
            (number, time.time(), call_id),
        )


def get_call_log(limit=200):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM modem_calls ORDER BY first_ring_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_call_log_entry(call_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM modem_calls WHERE id = ?", (call_id,))


def clear_call_log():
    with get_conn() as conn:
        conn.execute("DELETE FROM modem_calls")
