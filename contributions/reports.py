"""
reports.py - read-only queries behind the dashboard (and the sheet sync).

Filters (a plain dict, all optional):
  period     today | yesterday | week | month | all | custom   (default: all)
  start,end  'YYYY-MM-DD' (inclusive), for period=custom
  region_id  includes the region's sub-regions (a constituency rolls up its wards);
             the special value "none" selects contributions with no region yet
  account_id
  sender_id
  status     valid (default) | void | any

Date filtering is on the payment time printed in the SMS (paid_at), in the
configured timezone - never on when the Pi happened to process the message.
"""
from datetime import datetime, timedelta

from contributions import store

FMT = "%Y-%m-%d %H:%M:%S"


def resolve_period(period, start=None, end=None, tz=None, now=None):
    """-> (start_str, end_str) as a half-open [start, end) range of local
    'YYYY-MM-DD HH:MM:SS' strings; (None, None) for everything."""
    tz = tz or store.get_tz("Africa/Nairobi")
    now_l = datetime.fromtimestamp(now, tz) if now is not None else datetime.now(tz)
    midnight = now_l.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    period = period or "all"
    if period == "today":
        a, b = midnight, midnight + timedelta(days=1)
    elif period == "yesterday":
        a, b = midnight - timedelta(days=1), midnight
    elif period == "week":                       # ISO week: Monday start
        a = midnight - timedelta(days=midnight.weekday())
        b = a + timedelta(days=7)
    elif period == "month":
        a = midnight.replace(day=1)
        b = (a.replace(year=a.year + 1, month=1) if a.month == 12 else a.replace(month=a.month + 1))
    elif period == "custom":
        a = datetime.strptime(start, "%Y-%m-%d") if start else None
        b = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1) if end else None
        return (a.strftime(FMT) if a else None, b.strftime(FMT) if b else None)
    else:
        return None, None
    return a.strftime(FMT), b.strftime(FMT)


def _where(conn, f, tz, now=None, alias="c"):
    clauses, params = [], []
    a, b = resolve_period(f.get("period"), f.get("start"), f.get("end"), tz, now)
    if a:
        clauses.append(f"{alias}.paid_at >= ?")
        params.append(a)
    if b:
        clauses.append(f"{alias}.paid_at < ?")
        params.append(b)
    rid = f.get("region_id")
    if rid == "none":
        clauses.append(f"{alias}.region_id IS NULL")
    elif rid not in (None, ""):
        ids = store.region_ids(conn, int(rid))
        clauses.append(f"{alias}.region_id IN ({','.join('?' * len(ids))})")
        params += ids
    if f.get("account_id") not in (None, ""):
        clauses.append(f"{alias}.account_id = ?")
        params.append(int(f["account_id"]))
    if f.get("sender_id") not in (None, ""):
        clauses.append(f"{alias}.sender_id = ?")
        params.append(int(f["sender_id"]))
    status = f.get("status") or "valid"
    if status != "any":
        clauses.append(f"{alias}.status = ?")
        params.append(status)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _tz(conn):
    return store.get_tz(store.get_settings(conn)["timezone"])


def summary(conn, f, now=None):
    tz = _tz(conn)
    where, params = _where(conn, f, tz, now)
    r = conn.execute(
        "SELECT COALESCE(SUM(c.amount_cents),0) AS total_cents, COUNT(*) AS count, "
        "COUNT(DISTINCT c.sender_id) AS contributors, MIN(c.paid_at) AS first_at, MAX(c.paid_at) AS last_at "
        f"FROM contributions c{where}", params).fetchone()
    return dict(r)


def daily(conn, f, now=None):
    tz = _tz(conn)
    where, params = _where(conn, f, tz, now)
    return [dict(r) for r in conn.execute(
        "SELECT substr(c.paid_at,1,10) AS day, SUM(c.amount_cents) AS total_cents, COUNT(*) AS count "
        f"FROM contributions c{where} GROUP BY day ORDER BY day DESC LIMIT 400", params)]


def progress_table(conn, f, now=None):
    """One row per (region, account) that has contributions in the selected
    period OR an active target: collected in the period, plus the progress
    against the target (which uses the target's own period - see
    store.progress). Contributions without a region appear as 'Unassigned'."""
    tz = _tz(conn)
    now_ts = now if now is not None else datetime.now(tz).timestamp()
    today = datetime.fromtimestamp(now_ts, tz).strftime("%Y-%m-%d")
    where, params = _where(conn, f, tz, now)
    rows = {}
    for r in conn.execute(
            "SELECT c.region_id, c.account_id, SUM(c.amount_cents) AS collected, COUNT(*) AS count, "
            f"COUNT(DISTINCT c.sender_id) AS contributors, MAX(c.paid_at) AS last_at FROM contributions c{where} "
            "GROUP BY c.region_id, c.account_id", params):
        rows[(r["region_id"], r["account_id"])] = dict(r)
    if f.get("region_id") != "none":
        scope = None
        if f.get("region_id") not in (None, ""):
            scope = set(store.region_ids(conn, int(f["region_id"])))
        for t in conn.execute(
                "SELECT region_id, account_id FROM targets WHERE (starts_on IS NULL OR starts_on <= ?) AND (ends_on IS NULL OR ends_on >= ?)",
                (today, today)):
            if scope is not None and t["region_id"] not in scope:
                continue
            if f.get("account_id") not in (None, "") and t["account_id"] != int(f["account_id"]):
                continue
            rows.setdefault((t["region_id"], t["account_id"]),
                            {"region_id": t["region_id"], "account_id": t["account_id"], "collected": 0,
                             "count": 0, "contributors": 0, "last_at": None})
    regions = {r["id"]: r for r in store.list_regions(conn)}
    accounts = {a["id"]: a for a in store.list_accounts(conn)}
    out = []
    for (rid, aid), r in rows.items():
        reg, acc = regions.get(rid), accounts.get(aid)
        row = {
            "region_id": rid, "region": reg["name"] if reg else "Unassigned", "region_code": reg["code"] if reg else "",
            "parent": regions[reg["parent_id"]]["name"] if reg and reg["parent_id"] in regions else "",
            "account_id": aid, "account": store.account_label(acc) if acc else "", "account_ref": acc["account_ref"] if acc else "",
            "collected_cents": r["collected"], "count": r["count"], "contributors": r["contributors"], "last_at": r["last_at"],
            "target_cents": None, "percent": None, "progress_cents": None,
        }
        if rid is not None:
            prog = store.progress(conn, rid, aid, today)
            row.update(target_cents=prog["target_cents"], percent=prog["percent"], progress_cents=prog["total_cents"])
        out.append(row)
    out.sort(key=lambda x: (x["region_id"] is None, x["parent"].lower(), x["region"].lower(), x["account"].lower()))
    return out


def list_contributions(conn, f, limit=100, offset=0, now=None):
    tz = _tz(conn)
    where, params = _where(conn, f, tz, now)
    total = conn.execute(f"SELECT COUNT(*) FROM contributions c{where}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT c.id, c.txn_code, c.amount_cents, c.paid_at, c.status, c.region_id, c.account_id, c.sender_id, "
        "s.phone, s.name AS sender_name, g.name AS region, g.code AS region_code, a.label AS account_label, a.account_ref "
        "FROM contributions c JOIN senders s ON s.id = c.sender_id JOIN accounts a ON a.id = c.account_id "
        f"LEFT JOIN regions g ON g.id = c.region_id{where} ORDER BY c.paid_at DESC, c.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset]).fetchall()
    return [dict(r) for r in rows], total


def attention(conn):
    """Everything a person should look at: senders whose payments have no
    region yet, messages that were rejected or looked like payments but
    couldn't be read, and accounts first seen in a message that still need
    a project label."""
    unassigned = [dict(r) for r in conn.execute(
        "SELECT s.id, s.phone, s.name, COUNT(c.id) AS contributions, COALESCE(SUM(c.amount_cents),0) AS total_cents, "
        "MAX(c.paid_at) AS last_paid_at FROM senders s JOIN contributions c ON c.sender_id = s.id "
        "WHERE s.region_id IS NULL AND c.status = 'valid' GROUP BY s.id ORDER BY total_cents DESC")]
    messages = [dict(r) for r in conn.execute(
        "SELECT l.id, l.outcome, l.detail, l.body, l.created_at, s.phone FROM ingest_log l "
        "LEFT JOIN senders s ON s.id = l.sender_id WHERE l.review = 'open' ORDER BY l.id DESC LIMIT 100")]
    new_accounts = [dict(r) for r in conn.execute(
        "SELECT a.id, a.account_ref, p.number AS paybill_number FROM accounts a JOIN paybills p ON p.id = a.paybill_id "
        "WHERE a.auto_created = 1 ORDER BY a.id")]
    duplicates = conn.execute("SELECT COUNT(*) FROM duplicate_attempts WHERE created_at > strftime('%s','now') - 7*86400").fetchone()[0]
    return {"unassigned_senders": unassigned, "messages": messages, "new_accounts": new_accounts, "duplicates_7d": duplicates}


def message_log(conn, outcome=None, limit=100):
    q = ("SELECT l.id, l.source, l.outcome, l.detail, l.body, l.created_at, l.review, s.phone FROM ingest_log l "
         "LEFT JOIN senders s ON s.id = l.sender_id")
    params = []
    if outcome:
        q += " WHERE l.outcome = ?"
        params.append(outcome)
    q += " ORDER BY l.id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, [*params, limit])]
