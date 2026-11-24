"""
replies.py - reply templates and the outbound reply queue.

Replies are DECIDED inside the ingest transaction (queue_reply) and SENT
afterwards by flush() on its own loop. That gives:
  - ingesting never waits on the modem
  - a reply that was decided is never lost to a crash, nor sent twice
  - bounded load: at most `max_inflight` replies are handed to the modem
    daemon at once, so a burst of 200 forwards can't flood it or starve a
    running campaign
  - no stale spam: a reply that couldn't be sent within
    reply_expire_minutes (modem down, SIM out of credit) is expired and its
    pending modem command cancelled, so it can't fire hours later.

flush() writes modem_commands rows through its OWN transaction's
connection - never db.enqueue_modem_command() (a second writer connection
while this one holds the write lock would just time out).
"""
import json
import re
import time

import modem_gateway
from contributions import store

PLACEHOLDER = re.compile(r"\{\s*([a-zA-Z0-9_]+)\s*\}")
COOLDOWN_KINDS = ("help", "region_unknown_soft")


def fmt_money(cents):
    if cents is None:
        return ""
    return f"{cents // 100:,}" if cents % 100 == 0 else f"{cents / 100:,.2f}"


def render(template, values):
    """Unknown {placeholders} are left as typed so a typo is visible in the
    template preview instead of silently vanishing."""
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values and values[m.group(1)] is not None
                           else (m.group(0) if m.group(1) not in values else ""), template or "")


def queue_reply(conn, sender, kind, template_key, values, settings, now, cooldown=False):
    """Returns the new reply id, or None when suppressed (replies off, blank
    template, blocked sender, per-hour cap, or cooldown for nag-prone kinds)."""
    if settings.get("replies_enabled") != "1" or sender.get("blocked"):
        return None
    tpl = (settings.get(template_key) or "").strip()
    if not tpl:
        return None
    cap = store.as_int(settings, "reply_max_per_hour", 0)
    if cap and conn.execute("SELECT COUNT(*) FROM outbound_replies WHERE sender_id = ? AND created_at > ?",
                            (sender["id"], now - 3600)).fetchone()[0] >= cap:
        return None
    if cooldown:
        hours = store.as_int(settings, "help_cooldown_hours", 0)
        if hours and conn.execute("SELECT 1 FROM outbound_replies WHERE sender_id = ? AND kind = ? AND created_at > ?",
                                  (sender["id"], kind, now - hours * 3600)).fetchone():
            return None
    cur = conn.execute(
        "INSERT INTO outbound_replies (sender_id, phone, kind, body, status, created_at, next_attempt_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
        (sender["id"], sender["phone"], kind, render(tpl, values), now, now))
    return cur.lastrowid


def flush(now=None, max_inflight=3, max_attempts=3):
    """One pass of the reply sender. Returns counts (handy for tests/status)."""
    now = now or time.time()
    counts = {"sent": 0, "failed": 0, "expired": 0, "submitted": 0, "retry": 0}
    modem_ok = modem_gateway.test_connection({})["ok"]   # read-only status check
    with store.tx() as conn:
        settings = store.get_settings(conn)
        expire = store.as_int(settings, "reply_expire_minutes", 1) * 60

        for r in conn.execute("SELECT * FROM outbound_replies WHERE status = 'submitted'").fetchall():
            cmd = conn.execute("SELECT status, result FROM modem_commands WHERE id = ?", (r["command_id"],)).fetchone()
            if cmd is None:
                conn.execute("UPDATE outbound_replies SET status='failed', error='modem command vanished' WHERE id=?", (r["id"],))
                counts["failed"] += 1
            elif cmd["status"] == "done":
                conn.execute("UPDATE outbound_replies SET status='sent', sent_at=? WHERE id=?", (now, r["id"]))
                counts["sent"] += 1
            elif cmd["status"] == "failed":
                if r["attempts"] >= max_attempts:
                    conn.execute("UPDATE outbound_replies SET status='failed', error=? WHERE id=?", (cmd["result"], r["id"]))
                    counts["failed"] += 1
                else:
                    conn.execute("UPDATE outbound_replies SET status='queued', error=?, next_attempt_at=? WHERE id=?",
                                 (cmd["result"], now + 30 * r["attempts"], r["id"]))
                    counts["retry"] += 1
            elif now - (r["submitted_at"] or now) > expire:
                # still not picked up/finished: cancel it ONLY if the daemon hasn't claimed it
                conn.execute("UPDATE modem_commands SET status='failed', result='expired (contributions reply)', completed_at=? "
                             "WHERE id=? AND status='pending'", (now, r["command_id"]))
                conn.execute("UPDATE outbound_replies SET status='expired', error='not sent in time' WHERE id=?", (r["id"],))
                counts["expired"] += 1

        counts["expired"] += conn.execute(
            "UPDATE outbound_replies SET status='expired', error='not sent in time' WHERE status='queued' AND created_at < ?",
            (now - expire,)).rowcount

        inflight = conn.execute("SELECT COUNT(*) FROM outbound_replies WHERE status='submitted'").fetchone()[0]
        room = max_inflight - inflight
        if room > 0 and modem_ok:
            for r in conn.execute("SELECT * FROM outbound_replies WHERE status='queued' AND COALESCE(next_attempt_at,0) <= ? "
                                  "ORDER BY id LIMIT ?", (now, room)).fetchall():
                cur = conn.execute("INSERT INTO modem_commands (command, payload_json, created_at) VALUES ('send_sms', ?, ?)",
                                   (json.dumps({"phone": r["phone"], "text": r["body"]}), now))
                conn.execute("UPDATE outbound_replies SET status='submitted', command_id=?, attempts=attempts+1, submitted_at=? WHERE id=?",
                             (cur.lastrowid, now, r["id"]))
                counts["submitted"] += 1
    return counts
