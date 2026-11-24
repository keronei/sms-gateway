"""
sync.py - the periodic push of contributions to a Google Sheet.

The Pi is usually offline (its only internet is the modem's PPP link, which
the daemon never opens on its own). So a sync is a WINDOW:

  1. is there already internet (Ethernet, or a PPP link someone asked for)?
       yes -> use it, leave the connection exactly as it was
       no  -> ask the daemon for a temporary session (ppp_sync_connect),
              wait for the link, push, then ask it to drop the link
              (ppp_disconnect) - but only if WE brought it up. A link the
              user requested is never torn down by the sync.
  2. push: rewrite the Contributions tab and the Summary tab
  3. record success only if nothing changed meanwhile that the push missed

It only goes online when data_version > synced_version, i.e. when there is
actually something new - an idle feature never burns data bundle.
Each push rewrites the whole tab rather than appending, so voids, region
back-fills and renames always converge on the next sync, and a half-failed
push is simply repeated.
"""
import socket
import threading
import time
from datetime import datetime

import db
from contributions import reports, sheets, store

CONNECT_TIMEOUT = 120      # seconds to wait for PPP to come up
NET_TIMEOUT = 25           # seconds for DNS/routing to work after PPP is up
COMMAND_TIMEOUT = 15       # seconds for the daemon to acknowledge a command
POLL = 1.0
RETRY_AFTER_FAILURE = 600  # seconds before trying again after a failed attempt

_LOCK = threading.Lock()


class SyncError(Exception):
    pass


def have_internet(timeout=5):
    try:
        socket.create_connection(("oauth2.googleapis.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def _sleep(s):
    time.sleep(s)


# ----------------------------------------------------------- modem window
def _run_daemon_command(name, timeout=COMMAND_TIMEOUT):
    cid = db.enqueue_modem_command(name, {})
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd = db.get_modem_command(cid)
        if cmd and cmd["status"] in ("done", "failed"):
            if cmd["status"] == "failed":
                raise SyncError(f"Modem daemon refused '{name}': {cmd.get('result')}")
            return cmd.get("result") or ""
        _sleep(POLL)
    with db.get_conn() as conn:   # don't let a late daemon act on a stale request
        conn.execute("UPDATE modem_commands SET status='failed', result='timed out (contributions sync)' "
                     "WHERE id=? AND status='pending'", (cid,))
    raise SyncError("The modem daemon did not respond - is modem-manager running?")


def _wait_until(cond, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        _sleep(POLL)
    return cond()


def with_internet(settings, fn):
    """Runs fn() with internet available, opening and closing a modem session
    around it if (and only if) that was necessary."""
    if have_internet():
        return fn()
    if settings.get("sheets_use_modem") != "1":
        raise SyncError("No internet connection, and syncing over the modem is turned off.")
    outcome = _run_daemon_command("ppp_sync_connect")
    owned = outcome == "started"
    try:
        up = _wait_until(lambda: (db.get_modem_status() or {}).get("ppp_state") == "connected", CONNECT_TIMEOUT)
        if not up:
            err = (db.get_modem_status() or {}).get("ppp_last_error")
            raise SyncError("The modem could not get online" + (f" ({err})" if err else "") + ".")
        if not _wait_until(have_internet, NET_TIMEOUT):
            raise SyncError("The modem is connected but Google is unreachable (no data bundle or DNS?).")
        return fn()
    finally:
        if owned:
            try:
                _run_daemon_command("ppp_disconnect")
            except SyncError:
                pass


# ------------------------------------------------------------------ tables
def _text(s):
    return "'" + s if s else ""      # leading apostrophe keeps Sheets from reading it as a number


def _mask(phone):
    return phone[:-6] + "****" + phone[-2:] if len(phone) > 8 else phone


def build_tables(conn, settings):
    """-> (contributions_rows, summary_rows), header rows included."""
    mask = settings["sheets_mask_phones"] == "1"
    parents = {r["id"]: r["name"] for r in store.list_regions(conn)}
    contrib = [["Txn code", "Paid at", "Amount (Ksh)", "Sender phone", "Sender name", "Region", "Parent region",
                "Account", "Account no.", "Status", "Recorded at"]]
    tz = store.get_tz(settings["timezone"])
    rows, _ = reports.list_contributions(conn, {"status": "any"}, limit=10 ** 9)
    region_parent = {r["id"]: parents.get(r["parent_id"], "") for r in store.list_regions(conn)}
    created = {r["id"]: r["created_at"] for r in conn.execute("SELECT id, created_at FROM contributions")}
    for r in reversed(rows):   # oldest first
        phone = _mask(r["phone"]) if mask else r["phone"]
        contrib.append([
            _text(r["txn_code"]), r["paid_at"], r["amount_cents"] / 100, _text(phone), r["sender_name"] or "",
            r["region"] or "Unassigned", region_parent.get(r["region_id"], ""),
            r["account_label"] or r["account_ref"], _text(r["account_ref"]), r["status"],
            datetime.fromtimestamp(created.get(r["id"], 0), tz).strftime("%Y-%m-%d %H:%M:%S"),
        ])
    now_s = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    summary = [["Region", "Parent region", "Account", "Account no.", "Collected (Ksh)", "Contributions", "Contributors",
                "Target (Ksh)", "Progress %", "Last payment", "Synced at"]]
    for p in reports.progress_table(conn, {}):
        summary.append([
            p["region"], p["parent"], p["account"], _text(p["account_ref"]), p["collected_cents"] / 100, p["count"],
            p["contributors"], p["target_cents"] / 100 if p["target_cents"] else "",
            p["percent"] if p["percent"] is not None else "", p["last_at"] or "", now_s])
    return contrib, summary


def _client(settings):
    if not settings["sheets_spreadsheet_id"].strip():
        raise SyncError("No spreadsheet ID is set.")
    if not settings["sheets_service_account_json"].strip():
        raise SyncError("No service account key is set.")
    try:
        sa = sheets.parse_service_account(settings["sheets_service_account_json"])
    except sheets.SheetsError as e:
        raise SyncError(str(e))
    return sheets.SheetsClient(sa, settings["sheets_spreadsheet_id"].strip())


# --------------------------------------------------------------- orchestration
def status(conn, now=None):
    now = now or time.time()
    s = store.get_settings(conn)
    st = dict(conn.execute("SELECT * FROM sheet_sync_state WHERE id = 1").fetchone())
    st["pending"] = st["data_version"] > st["synced_version"]
    st["configured"] = bool(s["sheets_spreadsheet_id"].strip() and s["sheets_service_account_json"].strip())
    st["enabled"] = s["sheets_enabled"] == "1"
    st["running"] = _LOCK.locked()
    try:
        st["client_email"] = sheets.parse_service_account(s["sheets_service_account_json"])["client_email"]
    except Exception:
        st["client_email"] = None
    interval = store.as_int(s, "sheets_interval_minutes", 1) * 60
    last = st["last_attempt_at"] or 0
    failed = st["last_error"] and (st["last_success_at"] or 0) < last
    st["next_attempt_at"] = (last + (RETRY_AFTER_FAILURE if failed else interval)) if last else now
    return st


def maybe_run(now=None):
    """Called every ~30s by the worker; does nothing unless a sync is due."""
    now = now or time.time()
    with db.get_conn() as conn:
        s = store.get_settings(conn)
        if s["enabled"] != "1" or s["sheets_enabled"] != "1":
            return None
        st = status(conn, now)
    if not st["configured"] or not st["pending"] or now < st["next_attempt_at"]:
        return None
    return run_sync(now=now)


def run_sync(force=False, now=None):
    """One sync attempt. Returns {"ok", "message"|"error", ...}. Never raises."""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "A sync is already running."}
    now = now or time.time()
    try:
        with store.tx() as conn:
            settings = store.get_settings(conn)
            conn.execute("UPDATE sheet_sync_state SET last_attempt_at = ? WHERE id = 1", (now,))
            version = conn.execute("SELECT data_version FROM sheet_sync_state WHERE id = 1").fetchone()[0]
        try:
            client = _client(settings)

            def push():
                with db.get_conn() as c:
                    contrib, summary = build_tables(c, settings)
                ct, st = settings["sheets_contributions_tab"] or "Contributions", settings["sheets_summary_tab"] or "Summary"
                client.ensure_tabs([ct, st])
                client.write_table(ct, contrib)
                client.write_table(st, summary)
                return len(contrib) - 1, len(summary) - 1

            n_rows, n_summary = with_internet(settings, push)
            msg = f"Synced {n_rows} contribution(s) and {n_summary} summary row(s)."
            with store.tx() as conn:
                conn.execute("UPDATE sheet_sync_state SET synced_version = ?, last_success_at = ?, last_error = NULL, last_result = ? WHERE id = 1",
                             (version, time.time(), msg))
            return {"ok": True, "message": msg}
        except (SyncError, sheets.SheetsError) as e:
            err = str(e)
        except Exception as e:                       # network blips, JSON, etc.
            err = f"{type(e).__name__}: {e}"
        with store.tx() as conn:
            conn.execute("UPDATE sheet_sync_state SET last_error = ? WHERE id = 1", (err,))
        return {"ok": False, "error": err}
    finally:
        _LOCK.release()


def test_connection():
    """Opens the same window (if needed) and just reads the sheet's title."""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "A sync is already running."}
    try:
        with db.get_conn() as conn:
            settings = store.get_settings(conn)
        try:
            client = _client(settings)
            title = with_internet(settings, client.get_title)
            return {"ok": True, "message": f"Connected to spreadsheet \"{title}\"."}
        except (SyncError, sheets.SheetsError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _LOCK.release()
