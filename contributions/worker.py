"""
worker.py - the feature's background threads (all daemon threads, started
once from app.py, all no-ops while the feature is switched off):

  ingest  every poll_interval_seconds: pick up modem_inbox rows that have no
          ingest_log entry yet and run each through service.process_message()
          in its own transaction. Because "not yet ingested" is derived from
          ingest_log rather than a cursor that moves forward, this single
          loop is ALSO the "periodic check for missed messages": anything
          the daemon delivered late, or that failed once, is picked up on
          the next pass.
  replies every 2s: hand queued replies to the modem daemon (replies.flush)
  sync    every 30s: decide whether the Google-Sheets sync is due (sync.py)

Enabling the feature only considers messages that arrive AFTER the switch
(inbox_min_id is set to the current newest inbox id); older ones can be
pulled in deliberately with process_backlog().
"""
import logging
import threading
import time

import db
from contributions import replies, service, store

log = logging.getLogger("contributions")
_stop = threading.Event()
_threads = []


def _max_inbox_id(conn):
    r = conn.execute("SELECT COALESCE(MAX(id), 0) FROM modem_inbox").fetchone()
    return r[0]


def set_enabled(enabled):
    """Master switch. Returns {"enabled", "skipped_messages"}: how many inbox
    messages that arrived while the feature was off were NOT picked up, so the
    UI can offer to process them."""
    with store.tx() as conn:
        was = store.is_enabled(conn)
        skipped = 0
        if enabled and not was:
            settings = store.get_settings(conn)
            old_min = store.as_int(settings, "inbox_min_id", 0)
            newest = _max_inbox_id(conn)
            skipped = conn.execute(
                "SELECT COUNT(*) FROM modem_inbox i WHERE i.id > ? AND NOT EXISTS "
                "(SELECT 1 FROM ingest_log l WHERE l.source='sms' AND l.source_ref = CAST(i.id AS TEXT))",
                (old_min,)).fetchone()[0]
            store.set_settings(conn, {"inbox_min_id": newest})
        store.set_settings(conn, {"enabled": "1" if enabled else "0"})
    return {"enabled": bool(enabled), "skipped_messages": skipped}


def process_backlog(from_inbox_id=0):
    """Make the worker also consider older inbox messages (id > from_inbox_id)."""
    with store.tx() as conn:
        store.set_settings(conn, {"inbox_min_id": max(0, int(from_inbox_id))})
        n = conn.execute(
            "SELECT COUNT(*) FROM modem_inbox i WHERE i.id > ? AND NOT EXISTS "
            "(SELECT 1 FROM ingest_log l WHERE l.source='sms' AND l.source_ref = CAST(i.id AS TEXT))",
            (max(0, int(from_inbox_id)),)).fetchone()[0]
    return n


def reprocess_failed():
    """Re-queue messages that produced no contribution because of a fixable
    problem (unknown paybill, unreadable format, parser error, crash) by
    removing their ingest_log row - the next pass re-reads them from
    modem_inbox. Fully-recorded or partly-recorded messages are never
    touched, so nothing can be double-counted."""
    with store.tx() as conn:
        cur = conn.execute(
            "DELETE FROM ingest_log WHERE source='sms' AND outcome IN ('rejected','wrong_paybill','error') "
            "AND id NOT IN (SELECT ingest_id FROM contributions WHERE ingest_id IS NOT NULL)")
        return cur.rowcount


def ingest_once(limit=50):
    """One pass. Returns the number of messages processed."""
    with db.get_conn() as conn:
        settings = store.get_settings(conn)
        if settings["enabled"] != "1":
            return 0
        rows = [dict(r) for r in conn.execute(
            "SELECT i.id, i.sender, i.body, i.received_at FROM modem_inbox i WHERE i.id > ? AND NOT EXISTS "
            "(SELECT 1 FROM ingest_log l WHERE l.source='sms' AND l.source_ref = CAST(i.id AS TEXT)) ORDER BY i.id LIMIT ?",
            (store.as_int(settings, "inbox_min_id", 0), limit))]
    done = 0
    for row in rows:
        try:
            with store.tx() as conn:
                service.process_message(conn, "sms", row["id"], row["sender"], row["body"], row["received_at"])
        except Exception as e:      # one bad message must never wedge the queue behind it
            log.exception("ingest failed for inbox id %s", row["id"])
            try:
                with store.tx() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO ingest_log (source, source_ref, phone_raw, body, received_at, outcome, detail, review, created_at) "
                        "VALUES ('sms', ?, ?, ?, ?, 'error', ?, 'open', ?)",
                        (str(row["id"]), row["sender"], row["body"], row["received_at"], f"{type(e).__name__}: {e}", time.time()))
            except Exception:
                log.exception("could not record ingest error")
        done += 1
    return done


def _loop(name, interval_fn, fn):
    while not _stop.is_set():
        try:
            fn()
        except Exception:
            log.exception("contributions %s loop error", name)
        _stop.wait(interval_fn())


def start():
    """Idempotent. Skipped entirely when CONTRIB_DISABLE_WORKER=1 (tests)."""
    import os
    if _threads or os.environ.get("CONTRIB_DISABLE_WORKER") == "1":
        return
    from contributions import sync

    def poll_interval():
        try:
            with db.get_conn() as conn:
                return max(1, store.as_int(store.get_settings(conn), "poll_interval_seconds", 1))
        except Exception:
            return 5

    _stop.clear()
    specs = [
        ("ingest", poll_interval, ingest_once),
        ("replies", lambda: 2, _flush_if_enabled),
        ("sync", lambda: 30, sync.maybe_run),
    ]
    for name, interval, fn in specs:
        t = threading.Thread(target=_loop, args=(name, interval, fn), name=f"contrib-{name}", daemon=True)
        t.start()
        _threads.append(t)


def _flush_if_enabled():
    with db.get_conn() as conn:
        if not store.is_enabled(conn):
            return
    replies.flush()


def stop():
    _stop.set()
    for t in _threads:
        t.join(timeout=3)
    _threads.clear()
