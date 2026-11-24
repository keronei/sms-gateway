"""
api.py - Flask blueprint for the contributions feature (/api/contrib/...).

Optional PIN: when a PIN is set, every endpoint except /auth/* requires the
`contrib_auth` cookie issued by /auth/login. Tokens live in process memory
(this app is single-process by design) and expire after 12 hours; five
wrong PINs lock that client out for a minute. The PIN is stored only as a
salted PBKDF2 hash. This is a convenience lock for a LAN dashboard, not a
substitute for network-level protection.
"""
import csv
import functools
import hashlib
import hmac
import io
import secrets
import sqlite3
import time

from flask import Blueprint, Response, jsonify, request

import db
from contributions import parser, replies, reports, service, store, sync, worker

bp = Blueprint("contrib", __name__, url_prefix="/api/contrib")

COOKIE = "contrib_auth"
TOKEN_TTL = 12 * 3600
_tokens = {}          # token -> expiry
_fails = {}           # client ip -> (count, locked_until)


# --------------------------------------------------------------------- pin
def _hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${h}"


def _check_pin(pin, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(_hash_pin(pin, salt), stored)


def _pin_hash():
    with db.get_conn() as conn:
        return store.get_settings(conn)["pin_hash"]


def _authenticated():
    t = request.cookies.get(COOKIE)
    exp = _tokens.get(t)
    if exp and exp > time.time():
        return True
    _tokens.pop(t, None)
    return False


@bp.before_request
def _gate():
    if request.path.startswith("/api/contrib/auth/"):
        return None
    if _pin_hash() and not _authenticated():
        return jsonify({"error": "PIN required.", "pin_required": True}), 401
    return None


def _api(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (ValueError, sync.sheets.SheetsError) as e:
            return jsonify({"error": str(e)}), 400
        except sqlite3.IntegrityError:
            # a reference to something that doesn't exist (or was just deleted)
            return jsonify({"error": "That refers to a region, account or sender that no longer exists. Refresh and try again."}), 400
    return wrapper


@bp.route("/auth/status")
def auth_status():
    return jsonify({"pin_required": bool(_pin_hash()), "authenticated": _authenticated() or not _pin_hash()})


@bp.route("/auth/login", methods=["POST"])
def auth_login():
    ip = request.remote_addr or "?"
    count, locked_until = _fails.get(ip, (0, 0))
    if locked_until > time.time():
        return jsonify({"error": "Too many attempts. Wait a minute and try again."}), 429
    stored = _pin_hash()
    pin = str((request.get_json(silent=True) or {}).get("pin") or "")
    if stored and not _check_pin(pin, stored):
        count += 1
        _fails[ip] = (count, time.time() + 60 if count >= 5 else 0)
        return jsonify({"error": "Wrong PIN."}), 401
    _fails.pop(ip, None)
    token = secrets.token_urlsafe(24)
    _tokens[token] = time.time() + TOKEN_TTL
    resp = jsonify({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=TOKEN_TTL, httponly=True, samesite="Lax")
    return resp


@bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    _tokens.pop(request.cookies.get(COOKIE), None)
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@bp.route("/auth/pin", methods=["POST"])
@_api
def auth_set_pin():
    """Set, change or clear (empty string) the PIN. Changing/clearing an
    existing PIN requires being logged in."""
    stored = _pin_hash()
    if stored and not _authenticated():
        return jsonify({"error": "PIN required.", "pin_required": True}), 401
    pin = str((request.get_json(silent=True) or {}).get("pin") or "").strip()
    if pin and (len(pin) < 4 or len(pin) > 32):
        raise ValueError("A PIN must be 4-32 characters.")
    with store.tx() as conn:
        store.set_settings(conn, {"pin_hash": _hash_pin(pin) if pin else ""})
    _tokens.clear()
    return jsonify({"ok": True, "pin_set": bool(pin)})


# ---------------------------------------------------------------- settings
def _public_settings(s):
    out = {k: v for k, v in s.items() if k not in store.SECRET_KEYS}
    out["pin_set"] = bool(s["pin_hash"])
    out["sheets_service_account_set"] = bool(s["sheets_service_account_json"].strip())
    try:
        out["sheets_client_email"] = sync.sheets.parse_service_account(s["sheets_service_account_json"])["client_email"]
    except Exception:
        out["sheets_client_email"] = None
    return out


@bp.route("/state")
def state():
    with db.get_conn() as conn:
        s = store.get_settings(conn)
        counts = {
            "contributions": conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0],
            "senders": conn.execute("SELECT COUNT(*) FROM senders").fetchone()[0],
            "regions": conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0],
            "queued_replies": conn.execute("SELECT COUNT(*) FROM outbound_replies WHERE status IN ('queued','submitted')").fetchone()[0],
            "attention": conn.execute("SELECT COUNT(*) FROM ingest_log WHERE review='open'").fetchone()[0],
            "unprocessed_inbox": conn.execute(
                "SELECT COUNT(*) FROM modem_inbox i WHERE i.id > ? AND NOT EXISTS (SELECT 1 FROM ingest_log l "
                "WHERE l.source='sms' AND l.source_ref = CAST(i.id AS TEXT))", (store.as_int(s, "inbox_min_id", 0),)).fetchone()[0],
        }
        return jsonify({"enabled": s["enabled"] == "1", "settings": _public_settings(s), "counts": counts,
                        "default_templates": store.DEFAULT_TEMPLATES, "default_pattern": parser.DEFAULT_PATTERN})


@bp.route("/enable", methods=["POST"])
@_api
def enable():
    return jsonify(worker.set_enabled(bool((request.get_json(silent=True) or {}).get("enabled"))))


@bp.route("/backlog", methods=["POST"])
@_api
def backlog():
    n = worker.process_backlog(int((request.get_json(silent=True) or {}).get("from_inbox_id", 0)))
    return jsonify({"queued_messages": n})


@bp.route("/reprocess", methods=["POST"])
@_api
def reprocess():
    return jsonify({"requeued": worker.reprocess_failed()})


@bp.route("/settings", methods=["POST"])
@_api
def save_settings():
    body = request.get_json(silent=True) or {}
    updates = {}
    for k, v in body.items():
        if k in ("enabled", "pin_hash", "inbox_min_id") or k not in store.DEFAULTS:
            continue
        if k == "sheets_service_account_json":
            if not str(v).strip():
                continue                      # blank = "leave the stored key alone"
            sync.sheets.parse_service_account(v)
        if k == "parser_pattern":
            parser.compile_pattern(v)
        if k == "timezone":
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(v)
            except Exception:
                raise ValueError(f"Unknown timezone '{v}'. Use e.g. Africa/Nairobi.")
        if k == "sheets_spreadsheet_id" and "/d/" in str(v):
            v = str(v).split("/d/")[1].split("/")[0]       # accept a pasted sheet URL
        if k in ("poll_interval_seconds", "max_age_days", "date_grace_hours", "reply_max_per_hour", "reply_expire_minutes",
                 "region_prompt_max", "help_cooldown_hours", "sheets_interval_minutes"):
            try:
                if int(float(v)) < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"'{k}' must be a non-negative number.")
            v = int(float(v))
        if k in ("replies_enabled", "sheets_enabled", "sheets_use_modem", "sheets_mask_phones"):
            v = "1" if v in (True, 1, "1", "true", "on") else "0"
        updates[k] = v
    with store.tx() as conn:
        store.set_settings(conn, updates)
        if "sheets_mask_phones" in updates:
            store.bump_version(conn)
    return state()


@bp.route("/parser/test", methods=["POST"])
@_api
def parser_test():
    body = request.get_json(silent=True) or {}
    with db.get_conn() as conn:
        pattern = body.get("pattern") or store.get_settings(conn)["parser_pattern"]
    found = parser.parse_payments(body.get("body") or "", pattern)
    return jsonify({"matches": [{"code": p.code, "amount": replies.fmt_money(p.amount_cents), "account": p.account_ref,
                                 "paybill": p.paybill, "account_name": p.account_name, "paid_at": p.paid_at, "error": p.error}
                                for p in found]})


@bp.route("/templates/preview", methods=["POST"])
def template_preview():
    sample = {"amount": "200", "count": 1, "code": "UEOQP5BEC6", "account": "Building Fund", "account_ref": "133***6248",
              "region": "Karen", "region_code": "KAR", "region_total": "31,000", "target": "200,000", "percent": "15.5",
              "sender_name": "Jane", "example_code": "KAR", "suggestions": " - did you mean Karen (KAR)?", "date": "2026-05-24"}
    text = replies.render((request.get_json(silent=True) or {}).get("template") or "", sample)
    n = len(text)
    segs = 0 if not n else (1 if n <= 160 else -(-n // 153))
    return jsonify({"text": text, "chars": n, "segments": segs})


# ------------------------------------------------- regions/accounts/targets
@bp.route("/regions", methods=["GET", "POST"])
@_api
def regions():
    if request.method == "GET":
        with db.get_conn() as conn:
            return jsonify(store.list_regions(conn))
    with store.tx() as conn:
        rid = store.save_region(conn, request.get_json(silent=True) or {})
    return jsonify({"id": rid})


@bp.route("/regions/<int:rid>", methods=["PUT", "DELETE"])
@_api
def region_item(rid):
    with store.tx() as conn:
        if request.method == "DELETE":
            store.delete_region(conn, rid)
        else:
            store.save_region(conn, request.get_json(silent=True) or {}, rid)
    return jsonify({"ok": True})


@bp.route("/regions/bulk", methods=["POST"])
@_api
def regions_bulk():
    """One region per line: `Name, CODE` or `Name, CODE, PARENT_CODE`
    (parents must already exist or appear earlier in the list)."""
    text = (request.get_json(silent=True) or {}).get("text") or ""
    kind = (request.get_json(silent=True) or {}).get("kind") or "ward"
    created, errors = 0, []
    with store.tx() as conn:
        for n, line in enumerate(text.splitlines(), 1):
            parts = [p.strip() for p in next(csv.reader([line])) if p.strip()] if line.strip() else []
            if not parts:
                continue
            try:
                if len(parts) < 2:
                    raise ValueError("expected 'Name, CODE'")
                parent_id = None
                if len(parts) > 2:
                    r = conn.execute("SELECT id FROM regions WHERE code = ?", (parts[2],)).fetchone()
                    if not r:
                        raise ValueError(f"parent code '{parts[2]}' not found")
                    parent_id = r[0]
                store.save_region(conn, {"name": parts[0], "code": parts[1], "parent_id": parent_id, "kind": kind})
                created += 1
            except ValueError as e:
                errors.append(f"line {n}: {e}")
    return jsonify({"created": created, "errors": errors})


@bp.route("/paybills", methods=["GET", "POST"])
@_api
def paybills():
    if request.method == "GET":
        with db.get_conn() as conn:
            return jsonify(store.list_paybills(conn))
    with store.tx() as conn:
        return jsonify({"id": store.save_paybill(conn, request.get_json(silent=True) or {})})


@bp.route("/paybills/<int:pid>", methods=["PUT"])
@_api
def paybill_item(pid):
    with store.tx() as conn:
        store.save_paybill(conn, request.get_json(silent=True) or {}, pid)
    return jsonify({"ok": True})


@bp.route("/accounts", methods=["GET", "POST"])
@_api
def accounts():
    if request.method == "GET":
        with db.get_conn() as conn:
            return jsonify(store.list_accounts(conn))
    with store.tx() as conn:
        return jsonify({"id": store.save_account(conn, request.get_json(silent=True) or {})})


@bp.route("/accounts/<int:aid>", methods=["PUT", "DELETE"])
@_api
def account_item(aid):
    with store.tx() as conn:
        if request.method == "DELETE":
            store.delete_account(conn, aid)
        else:
            store.save_account(conn, request.get_json(silent=True) or {}, aid)
    return jsonify({"ok": True})


@bp.route("/targets", methods=["GET", "POST"])
@_api
def targets():
    if request.method == "GET":
        with db.get_conn() as conn:
            return jsonify(store.list_targets(conn))
    with store.tx() as conn:
        return jsonify({"id": store.save_target(conn, request.get_json(silent=True) or {})})


@bp.route("/targets/<int:tid>", methods=["DELETE"])
@_api
def target_item(tid):
    with store.tx() as conn:
        store.delete_target(conn, tid)
    return jsonify({"ok": True})


# ----------------------------------------------------------------- senders
@bp.route("/senders")
def senders():
    with db.get_conn() as conn:
        return jsonify(store.search_senders(conn, request.args.get("q", ""), min(int(request.args.get("limit", 50)), 200)))


@bp.route("/senders/<int:sid>", methods=["PUT"])
@_api
def sender_item(sid):
    body = request.get_json(silent=True) or {}
    with store.tx() as conn:
        if not store.get_sender(conn, sid):
            raise ValueError("Sender not found.")
        store.update_sender(conn, sid, name=body.get("name"), blocked=body.get("blocked"))
    return jsonify({"ok": True})


@bp.route("/senders/<int:sid>/region", methods=["POST"])
@_api
def sender_region(sid):
    body = request.get_json(silent=True) or {}
    with store.tx() as conn:
        sender = store.get_sender(conn, sid)
        region = store.get_region(conn, body.get("region_id")) if body.get("region_id") else None
        if not sender or not region:
            raise ValueError("Sender or region not found.")
        n = store.assign_sender_region(conn, sid, region["id"], "dashboard")
        if body.get("notify"):
            settings = store.get_settings(conn)
            replies.queue_reply(conn, store.get_sender(conn, sid), "region_set", "tpl_region_set",
                                {"region": region["name"], "region_code": region["code"], "sender_name": sender.get("name") or ""},
                                settings, time.time())
    return jsonify({"ok": True, "backfilled": n})


# ----------------------------------------------------------- contributions
def _filters():
    a = request.args
    return {k: a.get(k) for k in ("period", "start", "end", "region_id", "account_id", "sender_id", "status") if a.get(k)}


@bp.route("/summary")
@_api
def summary():
    f = _filters()
    with db.get_conn() as conn:
        return jsonify({"summary": reports.summary(conn, f), "progress": reports.progress_table(conn, f),
                        "daily": reports.daily(conn, f)})


@bp.route("/contributions")
@_api
def contributions():
    limit = min(int(request.args.get("limit", 100)), 1000)
    with db.get_conn() as conn:
        rows, total = reports.list_contributions(conn, _filters(), limit, int(request.args.get("offset", 0)))
    return jsonify({"rows": rows, "total": total})


@bp.route("/contributions/export.csv")
@_api
def contributions_export():
    with db.get_conn() as conn:
        rows, _ = reports.list_contributions(conn, _filters(), limit=10 ** 9)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["txn_code", "paid_at", "amount", "phone", "sender_name", "region", "account", "account_ref", "status"])
    for r in rows:
        w.writerow([r["txn_code"], r["paid_at"], r["amount_cents"] / 100, r["phone"], r["sender_name"] or "",
                    r["region"] or "Unassigned", r["account_label"] or "", r["account_ref"], r["status"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=contributions.csv"})


@bp.route("/contributions/<int:cid>/status", methods=["POST"])
@_api
def contribution_status(cid):
    with store.tx() as conn:
        store.set_contribution_status(conn, cid, (request.get_json(silent=True) or {}).get("status"))
    return jsonify({"ok": True})


# --------------------------------------------------------------- attention
@bp.route("/attention")
def attention():
    with db.get_conn() as conn:
        return jsonify(reports.attention(conn))


@bp.route("/messages")
def messages():
    with db.get_conn() as conn:
        return jsonify(reports.message_log(conn, request.args.get("outcome") or None))


@bp.route("/messages/<int:mid>/dismiss", methods=["POST"])
def message_dismiss(mid):
    with store.tx() as conn:
        conn.execute("UPDATE ingest_log SET review='dismissed' WHERE id = ?", (mid,))
    return jsonify({"ok": True})


@bp.route("/accounts/<int:aid>/reviewed", methods=["POST"])
def account_reviewed(aid):
    with store.tx() as conn:
        conn.execute("UPDATE accounts SET auto_created = 0 WHERE id = ?", (aid,))
    return jsonify({"ok": True})


# --------------------------------------------------------------------- sync
@bp.route("/sync/status")
def sync_status():
    with db.get_conn() as conn:
        return jsonify(sync.status(conn))


@bp.route("/sync/run", methods=["POST"])
def sync_run():
    # runs in the request thread on purpose: the dashboard shows the result,
    # and the sync lock makes a second click harmless
    return jsonify(sync.run_sync(force=True))


@bp.route("/sync/test", methods=["POST"])
def sync_test():
    return jsonify(sync.test_connection())
