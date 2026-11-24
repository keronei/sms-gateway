"""
store.py - data access for the contributions feature.

Every function takes an open sqlite connection as its first argument so the
caller decides the transaction boundary: the ingest pipeline wraps a whole
message (sender upsert + contributions + queued replies) in ONE transaction
via tx(), the API uses db.get_conn() for simple reads/writes.
"""
import contextlib
import difflib
import re
import time

import db
from contributions import parser

# --------------------------------------------------------------- settings
DEFAULT_TEMPLATES = {
    "tpl_ack": "Your support of Ksh {amount} has been received. Your region's progress as at today is Ksh {region_total}. Thank you.",
    "tpl_ack_target": "Your support of Ksh {amount} has been received. {region} progress: Ksh {region_total} of Ksh {target} ({percent}%). Thank you.",
    "tpl_ack_unassigned": "Your support of Ksh {amount} has been received. Please reply with your ward code (e.g. {example_code}) so it is counted under your ward. Thank you.",
    "tpl_ack_unassigned_quiet": "Your support of Ksh {amount} has been received. Thank you.",
    "tpl_region_set": "Thank you. You are now registered under {region}.",
    "tpl_region_unknown": "Sorry, we could not find that ward. Please reply with your ward code{suggestions}.",
    "tpl_duplicate": "This payment ({code}) was already recorded. No further action is needed. Thank you.",
    "tpl_help": "We could not read that message. Please forward the full M-PESA confirmation SMS for your payment.",
}

DEFAULTS = {
    "enabled": "0",
    "inbox_min_id": "0",           # worker only considers modem_inbox rows with id > this
    "poll_interval_seconds": "5",
    "timezone": "Africa/Nairobi",
    "max_age_days": "120",         # reject a payment older than this vs. when the SMS arrived (0 = no limit)
    "date_grace_hours": "36",      # ...or dated further than this AFTER the SMS arrived
    "parser_pattern": parser.DEFAULT_PATTERN,
    "region_keywords": "region,ward",
    "replies_enabled": "1",
    "reply_max_per_hour": "20",
    "reply_expire_minutes": "30",
    "region_prompt_max": "3",
    "help_cooldown_hours": "6",
    "sheets_enabled": "0",
    "sheets_spreadsheet_id": "",
    "sheets_contributions_tab": "Contributions",
    "sheets_summary_tab": "Summary",
    "sheets_interval_minutes": "60",
    "sheets_use_modem": "1",
    "sheets_mask_phones": "0",
    "sheets_service_account_json": "",   # secret
    "pin_hash": "",                      # secret
    **DEFAULT_TEMPLATES,
}
SECRET_KEYS = {"sheets_service_account_json", "pin_hash"}
INTERNAL_KEYS = {"seeded"}


def get_settings(conn):
    s = dict(DEFAULTS)
    for r in conn.execute("SELECT key, value FROM contrib_settings"):
        if r["key"] in DEFAULTS:
            s[r["key"]] = r["value"]
    return s


def set_settings(conn, updates):
    for k, v in updates.items():
        if k not in DEFAULTS:
            raise ValueError(f"Unknown setting: {k}")
        conn.execute(
            "INSERT INTO contrib_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, str(v)))


def as_int(settings, key, minimum=None):
    try:
        v = int(float(settings.get(key, DEFAULTS.get(key, 0))))
    except (TypeError, ValueError):
        v = int(float(DEFAULTS.get(key, 0) or 0))
    return max(v, minimum) if minimum is not None else v


def get_tz(name):
    """The configured timezone. Falls back to a fixed UTC+3 (East Africa
    Time, no DST) if the system tz database is missing on the Pi."""
    from datetime import timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=3))


def first_region_code(conn):
    """An example code for the "reply with your ward code" message: a leaf
    region (a ward), never a constituency that merely has wards under it."""
    r = conn.execute(
        "SELECT code FROM regions g WHERE g.active = 1 AND NOT EXISTS (SELECT 1 FROM regions k WHERE k.parent_id = g.id AND k.active = 1) "
        "ORDER BY g.id LIMIT 1").fetchone()
    if not r:
        r = conn.execute("SELECT code FROM regions WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
    return r[0] if r else "WARD"


def is_enabled(conn):
    return get_settings(conn)["enabled"] == "1"


def bump_version(conn):
    conn.execute("UPDATE sheet_sync_state SET data_version = data_version + 1 WHERE id = 1")


@contextlib.contextmanager
def tx():
    """One write transaction taken up-front (BEGIN IMMEDIATE) so two writers
    queue on the lock instead of failing mid-way on a read->write upgrade."""
    conn = db._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- regions
def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()


def list_regions(conn, include_inactive=True):
    q = "SELECT * FROM regions" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY name COLLATE NOCASE"
    regions = [dict(r) for r in conn.execute(q)]
    aliases = {}
    for r in conn.execute("SELECT region_id, alias FROM region_aliases ORDER BY alias"):
        aliases.setdefault(r["region_id"], []).append(r["alias"])
    for r in regions:
        r["aliases"] = aliases.get(r["id"], [])
    return regions


def get_region(conn, region_id):
    r = conn.execute("SELECT * FROM regions WHERE id = ?", (region_id,)).fetchone()
    return dict(r) if r else None


def _set_aliases(conn, region_id, aliases):
    conn.execute("DELETE FROM region_aliases WHERE region_id = ?", (region_id,))
    for a in dict.fromkeys(x.strip() for x in (aliases or []) if x and x.strip()):
        conn.execute("INSERT INTO region_aliases (region_id, alias) VALUES (?, ?)", (region_id, a))


def save_region(conn, data, region_id=None):
    name = (data.get("name") or "").strip()
    code = (data.get("code") or "").strip()
    if not name or not code:
        raise ValueError("A region needs a name and a code.")
    if re.search(r"\s", code):
        raise ValueError("A region code cannot contain spaces.")
    parent_id = data.get("parent_id") or None
    if parent_id is not None:
        if region_id is not None and (int(parent_id) == region_id or int(parent_id) in region_ids(conn, region_id)):
            raise ValueError("A region cannot be its own parent or descendant.")
        if not get_region(conn, parent_id):
            raise ValueError("Parent region not found.")
    fields = (parent_id, (data.get("kind") or "ward").strip() or "ward", name, code, 1 if data.get("active", True) else 0)
    try:
        if region_id is None:
            cur = conn.execute(
                "INSERT INTO regions (parent_id, kind, name, code, active, created_at) VALUES (?,?,?,?,?,?)",
                (*fields, time.time()))
            region_id = cur.lastrowid
        else:
            conn.execute("UPDATE regions SET parent_id=?, kind=?, name=?, code=?, active=? WHERE id=?", (*fields, region_id))
        if "aliases" in data:
            _set_aliases(conn, region_id, data["aliases"])
    except Exception as e:
        if "UNIQUE" in str(e):
            raise ValueError("That region code or alias is already in use.")
        raise
    bump_version(conn)
    return region_id


def delete_region(conn, region_id):
    refs = conn.execute(
        "SELECT (SELECT COUNT(*) FROM contributions WHERE region_id = ?) + "
        "(SELECT COUNT(*) FROM senders WHERE region_id = ?) + "
        "(SELECT COUNT(*) FROM regions WHERE parent_id = ?)", (region_id, region_id, region_id)).fetchone()[0]
    if refs:
        raise ValueError("This region still has sub-regions, senders or contributions. Deactivate it instead.")
    conn.execute("DELETE FROM regions WHERE id = ?", (region_id,))
    bump_version(conn)


def region_ids(conn, region_id):
    """The region plus all its descendants (a constituency rolls up its wards)."""
    out, frontier = [int(region_id)], [int(region_id)]
    while frontier:
        marks = ",".join("?" * len(frontier))
        kids = [r[0] for r in conn.execute(f"SELECT id FROM regions WHERE parent_id IN ({marks})", frontier)]
        kids = [k for k in kids if k not in out]
        out.extend(kids)
        frontier = kids
    return out


def match_region(conn, text):
    """Resolve free text a sender replied with to ONE active region.
    Returns (region|None, suggestions[list of region]). Order of attempts:
    exact name/code/alias -> a single region named as whole words inside
    the text (longest name wins) -> fuzzy suggestions (never auto-accepted:
    a wrong ward is worse than asking again)."""
    t = _norm(text)
    if not t:
        return None, []
    index = []
    for r in conn.execute("SELECT id, name, code FROM regions WHERE active = 1"):
        index += [(_norm(r["name"]), r["id"]), (_norm(r["code"]), r["id"])]
    for r in conn.execute("SELECT a.alias, a.region_id FROM region_aliases a JOIN regions g ON g.id = a.region_id WHERE g.active = 1"):
        index.append((_norm(r["alias"]), r["region_id"]))
    index = [(k, rid) for k, rid in index if k]

    exact = {rid for k, rid in index if k == t}
    if len(exact) == 1:
        return get_region(conn, exact.pop()), []
    if len(exact) > 1:
        return None, [get_region(conn, rid) for rid in sorted(exact)][:3]

    padded = f" {t} "
    hits = [(k, rid) for k, rid in index if f" {k} " in padded]
    if hits:
        longest = max(len(k) for k, _ in hits)
        best = {rid for k, rid in hits if len(k) == longest}
        if len(best) == 1:
            return get_region(conn, best.pop()), []
        return None, [get_region(conn, rid) for rid in sorted(best)][:3]

    keys = [k for k, _ in index]
    suggestions, seen = [], set()
    for k in difflib.get_close_matches(t, keys, n=5, cutoff=0.6):
        for kk, rid in index:
            if kk == k and rid not in seen:
                seen.add(rid)
                suggestions.append(get_region(conn, rid))
    return None, suggestions[:3]


# --------------------------------------------------- paybills and accounts
def list_paybills(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM paybills ORDER BY number")]


def save_paybill(conn, data, paybill_id=None):
    number = re.sub(r"\D", "", data.get("number") or "")
    if not number:
        raise ValueError("A paybill needs a number.")
    try:
        if paybill_id is None:
            cur = conn.execute("INSERT INTO paybills (number, name, active, created_at) VALUES (?,?,?,?)",
                               (number, (data.get("name") or "").strip(), 1 if data.get("active", True) else 0, time.time()))
            paybill_id = cur.lastrowid
        else:
            conn.execute("UPDATE paybills SET number=?, name=?, active=? WHERE id=?",
                         (number, (data.get("name") or "").strip(), 1 if data.get("active", True) else 0, paybill_id))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise ValueError("That paybill number already exists.")
        raise
    bump_version(conn)
    return paybill_id


def find_paybill(conn, number):
    r = conn.execute("SELECT * FROM paybills WHERE number = ?", (number,)).fetchone()
    return dict(r) if r else None


def default_paybill(conn):
    r = conn.execute("SELECT * FROM paybills WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
    return dict(r) if r else None


def list_accounts(conn):
    return [dict(r) for r in conn.execute(
        "SELECT a.*, p.number AS paybill_number FROM accounts a JOIN paybills p ON p.id = a.paybill_id "
        "ORDER BY a.label COLLATE NOCASE, a.account_ref")]


def get_or_create_account(conn, paybill_id, account_ref, hint_name=None):
    """Looks the masked account number up (exact string, as printed in the
    SMS). A first-seen one is created and flagged auto_created so it shows
    up under "needs attention" until you give it a project label - nothing
    is lost or rejected just because it wasn't configured yet."""
    r = conn.execute("SELECT * FROM accounts WHERE paybill_id = ? AND account_ref = ?", (paybill_id, account_ref)).fetchone()
    if r:
        return dict(r), False
    cur = conn.execute(
        "INSERT INTO accounts (paybill_id, account_ref, label, auto_created, created_at) VALUES (?,?,?,1,?)",
        (paybill_id, account_ref, None, time.time()))
    bump_version(conn)
    return dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()), True


def save_account(conn, data, account_id=None):
    ref = (data.get("account_ref") or "").strip().upper()
    if not ref:
        raise ValueError("An account needs the account number as it appears in the SMS (e.g. 133***6248).")
    paybill_id = data.get("paybill_id")
    if not paybill_id:
        pb = default_paybill(conn)
        if not pb:
            raise ValueError("Add a paybill first.")
        paybill_id = pb["id"]
    vals = (paybill_id, ref, (data.get("label") or "").strip(), 1 if data.get("active", True) else 0)
    try:
        if account_id is None:
            cur = conn.execute("INSERT INTO accounts (paybill_id, account_ref, label, active, auto_created, created_at) VALUES (?,?,?,?,0,?)",
                               (*vals, time.time()))
            account_id = cur.lastrowid
        else:
            conn.execute("UPDATE accounts SET paybill_id=?, account_ref=?, label=?, active=?, auto_created=0 WHERE id=?", (*vals, account_id))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise ValueError("That account already exists for this paybill.")
        raise
    bump_version(conn)
    return account_id


def delete_account(conn, account_id):
    if conn.execute("SELECT COUNT(*) FROM contributions WHERE account_id = ?", (account_id,)).fetchone()[0]:
        raise ValueError("This account has contributions. Deactivate it instead.")
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    bump_version(conn)


def account_label(account):
    return (account.get("label") or "").strip() or account.get("account_ref") or ""


# ---------------------------------------------------------------- targets
def list_targets(conn):
    return [dict(r) for r in conn.execute(
        "SELECT t.*, r.name AS region_name, a.label AS account_label, a.account_ref FROM targets t "
        "JOIN regions r ON r.id = t.region_id JOIN accounts a ON a.id = t.account_id "
        "ORDER BY r.name COLLATE NOCASE, a.label")]


def save_target(conn, data):
    try:
        cents = int(round(float(str(data.get("amount")).replace(",", "")) * 100))
    except (TypeError, ValueError):
        raise ValueError("Target amount must be a number.")
    if cents <= 0:
        raise ValueError("Target amount must be greater than zero.")
    starts, ends = (data.get("starts_on") or None), (data.get("ends_on") or None)
    if starts and ends and starts > ends:
        raise ValueError("Target start date is after its end date.")
    if not data.get("region_id") or not get_region(conn, data["region_id"]):
        raise ValueError("Choose a region for the target.")
    if not data.get("account_id") or not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (data["account_id"],)).fetchone():
        raise ValueError("Choose an account for the target.")
    cur = conn.execute(
        "INSERT INTO targets (region_id, account_id, amount_cents, starts_on, ends_on, created_at) VALUES (?,?,?,?,?,?)",
        (data["region_id"], data["account_id"], cents, starts, ends, time.time()))
    bump_version(conn)
    return cur.lastrowid


def delete_target(conn, target_id):
    conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
    bump_version(conn)


def _active_target(conn, region_id, account_id, on_date):
    return conn.execute(
        "SELECT * FROM targets WHERE region_id = ? AND account_id = ? "
        "AND (starts_on IS NULL OR starts_on <= ?) AND (ends_on IS NULL OR ends_on >= ?) "
        "ORDER BY COALESCE(starts_on, '') DESC, id DESC LIMIT 1",
        (region_id, account_id, on_date, on_date)).fetchone()


def progress(conn, region_id, account_id, on_date):
    """Collected vs target for one region (incl. its sub-regions) and account.
    The region's OWN active target wins and its period bounds the total; with
    no own target, the sub-regions' active targets are summed against the
    all-time total. No target at all -> target/percent are None (the
    dashboard and SMS then just show the running total)."""
    ids = region_ids(conn, region_id)
    marks = ",".join("?" * len(ids))
    own = _active_target(conn, region_id, account_id, on_date)
    sql = f"SELECT COALESCE(SUM(amount_cents), 0) FROM contributions WHERE status = 'valid' AND account_id = ? AND region_id IN ({marks})"
    params = [account_id, *ids]
    target_cents, period = None, None
    if own:
        target_cents, period = own["amount_cents"], (own["starts_on"], own["ends_on"])
        if own["starts_on"]:
            sql += " AND paid_at >= ?"
            params.append(own["starts_on"] + " 00:00:00")
        if own["ends_on"]:
            sql += " AND paid_at < date(?, '+1 day')"
            params.append(own["ends_on"])
    elif len(ids) > 1:
        total_t = 0
        for rid in ids:
            t = _active_target(conn, rid, account_id, on_date)
            total_t += t["amount_cents"] if t else 0
        target_cents = total_t or None
    total = conn.execute(sql, params).fetchone()[0]
    pct = round(total * 100 / target_cents, 1) if target_cents else None
    return {"total_cents": total, "target_cents": target_cents, "percent": pct, "period": period}


# ---------------------------------------------------------------- senders
def get_sender(conn, sender_id):
    r = conn.execute("SELECT s.*, g.name AS region_name FROM senders s LEFT JOIN regions g ON g.id = s.region_id WHERE s.id = ?",
                     (sender_id,)).fetchone()
    return dict(r) if r else None


def get_or_create_sender(conn, phone, now=None):
    r = conn.execute("SELECT * FROM senders WHERE phone = ?", (phone,)).fetchone()
    if r:
        return dict(r)
    conn.execute("INSERT OR IGNORE INTO senders (phone, created_at) VALUES (?, ?)", (phone, now or time.time()))
    bump_version(conn)
    return dict(conn.execute("SELECT * FROM senders WHERE phone = ?", (phone,)).fetchone())


def search_senders(conn, q="", limit=50):
    """Tolerant phone search: digits only, matched as a substring, so
    '0712 345', '712345' and '+254712345678' all find the same person."""
    digits = re.sub(r"\D", "", q or "")
    if digits.startswith("0"):
        digits = digits.lstrip("0")   # local format (0712...) vs stored international (+254712...)
    base = ("SELECT s.*, g.name AS region_name, "
            "(SELECT COUNT(*) FROM contributions c WHERE c.sender_id = s.id) AS contribution_count, "
            "(SELECT COALESCE(SUM(amount_cents),0) FROM contributions c WHERE c.sender_id = s.id AND c.status='valid') AS total_cents "
            "FROM senders s LEFT JOIN regions g ON g.id = s.region_id ")
    if digits:
        rows = conn.execute(base + "WHERE REPLACE(REPLACE(s.phone,'+',''),' ','') LIKE ? ORDER BY s.id DESC LIMIT ?",
                            (f"%{digits}%", limit))
    elif (q or "").strip():
        rows = conn.execute(base + "WHERE s.name LIKE ? ORDER BY s.id DESC LIMIT ?", (f"%{q.strip()}%", limit))
    else:
        rows = conn.execute(base + "ORDER BY s.id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def assign_sender_region(conn, sender_id, region_id, by, now=None):
    """Sets the sender's CURRENT region and back-fills their contributions
    that had none yet. Contributions already attributed to another region
    keep it. Returns how many contributions were back-filled."""
    now = now or time.time()
    conn.execute("UPDATE senders SET region_id=?, region_set_at=?, region_set_by=? WHERE id=?", (region_id, now, by, sender_id))
    cur = conn.execute("UPDATE contributions SET region_id=?, updated_at=? WHERE sender_id=? AND region_id IS NULL",
                       (region_id, now, sender_id))
    bump_version(conn)
    return cur.rowcount


def update_sender(conn, sender_id, name=None, blocked=None):
    if name is not None:
        conn.execute("UPDATE senders SET name=? WHERE id=?", (name.strip() or None, sender_id))
    if blocked is not None:
        conn.execute("UPDATE senders SET blocked=? WHERE id=?", (1 if blocked else 0, sender_id))
    bump_version(conn)


# ---------------------------------------------------------- contributions
def insert_contribution(conn, txn_code, sender, account_id, amount_cents, paid_at, ingest_id, now=None):
    """Raises sqlite3.IntegrityError if txn_code already exists - callers
    treat that as the duplicate signal, which is race-safe where a prior
    SELECT-then-INSERT would not be."""
    now = now or time.time()
    cur = conn.execute(
        "INSERT INTO contributions (txn_code, sender_id, account_id, region_id, amount_cents, paid_at, ingest_id, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?, 'valid', ?, ?)",
        (txn_code, sender["id"], account_id, sender.get("region_id"), amount_cents, paid_at, ingest_id, now, now))
    bump_version(conn)
    return cur.lastrowid


def set_contribution_status(conn, contribution_id, status):
    if status not in ("valid", "void"):
        raise ValueError("Status must be 'valid' or 'void'.")
    conn.execute("UPDATE contributions SET status=?, updated_at=? WHERE id=?", (status, time.time(), contribution_id))
    bump_version(conn)
