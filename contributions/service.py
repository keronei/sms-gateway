"""
service.py - the ingest pipeline: ONE inbound message in, decisions out.

process_message() runs inside the caller's transaction (see worker.py) and
is the only place that decides what a message means:

  not a real phone number (short code / sender name)   -> ignored, never replied to
  the "connect" keyword                                 -> ignored (the modem daemon handles it)
  blocked sender                                        -> logged only
  contains one or more payment confirmations            -> record each, dedupe by M-PESA ref, ack/duplicate replies
  looks like a region reply / region-change request     -> assign region (+ back-fill), confirm
  anything else                                         -> polite "help" reply, rate limited

Source-agnostic on purpose: SMS calls it with source="sms"; a WhatsApp
webhook later calls it with source="whatsapp" and its own message id.
"""
import re
import sqlite3
import time
from datetime import datetime

import merge
from contributions import parser, replies, store

# Messages the modem daemon itself acts on; they are not contributions or chat.
IGNORED_KEYWORDS = {"connect"}
LOOKS_LIKE_PAYMENT = re.compile(r"m-?pesa|ksh|kes\b|pay\s*bill|paybill|\bkcb\b", re.I)


def is_real_phone(raw):
    """A reply-able subscriber number. merge.normalize_phone alone is not
    enough: with a default country code set, a short code like 16120 becomes
    '+25416120' (8 digits) and would pass. Real numbers have no letters and
    either an explicit '+'/'00' prefix or at least 9 digits."""
    raw = (raw or "").strip()
    if not raw or re.search(r"[A-Za-z]", raw):
        return False
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") or raw.startswith("00"):
        return len(digits) >= 8
    return len(digits) >= 9


def _default_cc(conn):
    try:
        row = conn.execute("SELECT default_country_code FROM settings WHERE id = 1").fetchone()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def _paid_epoch(paid_at, tz):
    return datetime.strptime(paid_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()


def _sanity_problem(paid_at, received_at, settings, now, tz):
    # Judge the payment's date against when the SMS ARRIVED (network
    # timestamp), not the Pi's clock - the Pi has no battery-backed clock.
    ref = received_at or now
    paid = _paid_epoch(paid_at, tz)
    grace = store.as_int(settings, "date_grace_hours", 0) * 3600
    if paid > ref + grace:
        return "payment is dated in the future"
    max_age = store.as_int(settings, "max_age_days", 0)
    if max_age and paid < ref - max_age * 86400:
        return f"payment is older than {max_age} days"
    return None


def process_message(conn, source, source_ref, sender_raw, body, received_at=None, now=None):
    """Returns a result dict, or None if this (source, source_ref) was
    already processed (idempotency)."""
    now = now or time.time()
    settings = store.get_settings(conn)
    cur = conn.execute(
        "INSERT OR IGNORE INTO ingest_log (source, source_ref, phone_raw, body, received_at, outcome, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'processing', ?)",
        (source, str(source_ref), sender_raw, body, received_at, now))
    if cur.rowcount == 0:
        return None
    ingest_id = cur.lastrowid
    res = _dispatch(conn, settings, ingest_id, sender_raw, (body or "").strip(), received_at, now)
    conn.execute("UPDATE ingest_log SET sender_id=?, outcome=?, detail=?, review=? WHERE id=?",
                 (res.get("sender_id"), res["outcome"], res.get("detail"), "open" if res.get("review") else None, ingest_id))
    res["ingest_id"] = ingest_id
    return res


def _dispatch(conn, settings, ingest_id, sender_raw, body, received_at, now):
    if not is_real_phone(sender_raw):
        return {"outcome": "ignored", "detail": "sender is not a subscriber phone number"}
    phone, valid = merge.normalize_phone(sender_raw, _default_cc(conn))
    if not valid:
        return {"outcome": "ignored", "detail": "sender number could not be normalized"}
    if body.lower().strip(" .!") in IGNORED_KEYWORDS:
        return {"outcome": "ignored", "detail": "command keyword handled by the modem daemon"}

    sender = store.get_or_create_sender(conn, phone, now)
    if sender["blocked"]:
        return {"outcome": "blocked", "sender_id": sender["id"], "detail": "sender is blocked"}

    tz = store.get_tz(settings["timezone"])
    pattern = settings["parser_pattern"]
    try:
        payments = parser.parse_payments(body, pattern)
    except parser.PatternError as e:
        return {"outcome": "error", "sender_id": sender["id"], "detail": f"parser pattern is invalid: {e}", "review": True}

    if payments:
        return _handle_payments(conn, settings, ingest_id, sender, payments, received_at, now, tz)

    region_res = _handle_region_text(conn, settings, sender, body, now)
    if region_res:
        return region_res

    replies_made = []
    if replies.queue_reply(conn, sender, "help", "tpl_help", {}, settings, now, cooldown=True):
        replies_made.append("help")
    return {"outcome": "unparseable", "sender_id": sender["id"], "detail": body[:200], "replies": replies_made,
            "review": bool(LOOKS_LIKE_PAYMENT.search(body))}


# ---------------------------------------------------------------- payments
def _handle_payments(conn, settings, ingest_id, sender, payments, received_at, now, tz):
    new_by_account, dup_codes, problems, recorded = {}, [], [], []
    wrong_paybill_only = True
    for p in payments:
        if p.error:
            problems.append(f"{p.code}: {p.error}")
            wrong_paybill_only = False
            continue
        bad = _sanity_problem(p.paid_at, received_at, settings, now, tz)
        if bad:
            problems.append(f"{p.code}: {bad}")
            wrong_paybill_only = False
            continue
        pb = store.find_paybill(conn, p.paybill) if p.paybill else store.default_paybill(conn)
        if not pb or not pb["active"]:
            problems.append(f"{p.code}: paybill {p.paybill or '?'} is not accepted")
            continue
        wrong_paybill_only = False
        account, _created = store.get_or_create_account(conn, pb["id"], p.account_ref, p.account_name)
        if not account["active"]:
            problems.append(f"{p.code}: account {p.account_ref} is disabled")
            continue
        try:
            cid = store.insert_contribution(conn, p.code, sender, account["id"], p.amount_cents, p.paid_at, ingest_id, now)
        except sqlite3.IntegrityError:
            # txn_code UNIQUE is the double-entry guard (and what makes two
            # simultaneous forwards of one receipt safe)
            existing = conn.execute("SELECT id FROM contributions WHERE txn_code = ?", (p.code,)).fetchone()
            conn.execute("INSERT INTO duplicate_attempts (contribution_id, sender_id, ingest_id, created_at) VALUES (?,?,?,?)",
                         (existing["id"], sender["id"], ingest_id, now))
            dup_codes.append(p.code)
            continue
        recorded.append(cid)
        new_by_account.setdefault(account["id"], {"account": account, "items": []})["items"].append((cid, p))

    reply_kinds = []
    sender = store.get_sender(conn, sender["id"])   # fresh: region may be set
    asked_region = False
    today = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    for group in new_by_account.values():
        account, items = group["account"], group["items"]
        total = sum(p.amount_cents for _, p in items)
        values = {
            "amount": replies.fmt_money(total), "count": len(items), "code": ", ".join(p.code for _, p in items),
            "account": store.account_label(account), "account_ref": account["account_ref"],
            "sender_name": sender.get("name") or "", "example_code": store.first_region_code(conn),
            "date": items[0][1].paid_at[:10],
        }
        if sender["region_id"]:
            region = store.get_region(conn, sender["region_id"])
            prog = store.progress(conn, region["id"], account["id"], today)
            values.update({"region": region["name"], "region_code": region["code"],
                           "region_total": replies.fmt_money(prog["total_cents"]),
                           "target": replies.fmt_money(prog["target_cents"]) if prog["target_cents"] else "",
                           "percent": f"{prog['percent']:g}" if prog["percent"] is not None else ""})
            key = "tpl_ack_target" if prog["target_cents"] and (settings.get("tpl_ack_target") or "").strip() else "tpl_ack"
            kind = "ack"
        else:
            prompts_left = store.as_int(settings, "region_prompt_max", 0) - sender["region_prompts"]
            if prompts_left > 0 and not asked_region:
                key, kind, asked_region = "tpl_ack_unassigned", "ack_unassigned", True
                conn.execute("UPDATE senders SET region_prompts = region_prompts + 1 WHERE id = ?", (sender["id"],))
            else:
                key, kind = "tpl_ack_unassigned_quiet", "ack_unassigned_quiet"
        if replies.queue_reply(conn, sender, kind, key, values, settings, now):
            reply_kinds.append(kind)

    if dup_codes:
        if replies.queue_reply(conn, sender, "duplicate", "tpl_duplicate", {"code": ", ".join(dup_codes)}, settings, now):
            reply_kinds.append("duplicate")

    parts = []
    if recorded:
        parts.append(f"{len(recorded)} recorded")
    if dup_codes:
        parts.append(f"{len(dup_codes)} duplicate")
    if problems:
        parts.append("rejected: " + "; ".join(problems))
    if recorded:
        outcome = "recorded"
    elif dup_codes:
        outcome = "duplicate"
    else:
        outcome = "wrong_paybill" if wrong_paybill_only and problems else "rejected"
    return {"outcome": outcome, "sender_id": sender["id"], "detail": ", ".join(parts), "recorded": recorded,
            "duplicates": dup_codes, "replies": reply_kinds, "review": bool(problems)}


# ------------------------------------------------------------------ region
def _handle_region_text(conn, settings, sender, body, now):
    """A region reply (for senders without a region) or an explicit change
    request ("ward KAR") from one that has one. Returns a result dict, or
    None if this text isn't about regions at all."""
    keywords = [k.strip() for k in settings["region_keywords"].split(",") if k.strip()]
    candidate, has_keyword = body, False
    if keywords:
        m = re.match(r"^\s*(?:%s)\b[\s:.\-]*(.*)$" % "|".join(re.escape(k) for k in keywords), body, re.I | re.S)
        if m:
            candidate, has_keyword = m.group(1), True
    unassigned = sender["region_id"] is None
    if not unassigned and not has_keyword:
        return None            # a registered sender's random text must never move them
    if unassigned and not has_keyword and not sender["region_prompts"]:
        return None            # never asked this person for a ward, so "hello" isn't an answer
    candidate = candidate.strip()
    if len(candidate) > 60:
        return None
    if not candidate and not has_keyword:
        return None

    region, suggestions = store.match_region(conn, candidate) if candidate else (None, [])
    if region:
        backfilled = store.assign_sender_region(conn, sender["id"], region["id"], "sms", now)
        sender = store.get_sender(conn, sender["id"])
        made = []
        if replies.queue_reply(conn, sender, "region_set", "tpl_region_set",
                               {"region": region["name"], "region_code": region["code"], "sender_name": sender.get("name") or ""},
                               settings, now):
            made.append("region_set")
        return {"outcome": "region_set", "sender_id": sender["id"], "replies": made,
                "detail": f"{region['name']} ({backfilled} earlier contribution(s) back-filled)"}

    hint = ""
    if suggestions:
        hint = " - did you mean " + ", ".join(f"{s['name']} ({s['code']})" for s in suggestions) + "?"
    values = {"suggestions": hint}
    made = []
    # An explicit "ward xyz" always gets an answer; loose chatter from an
    # unassigned sender is answered at most once per cooldown.
    if replies.queue_reply(conn, sender, "region_unknown" if has_keyword else "region_unknown_soft",
                           "tpl_region_unknown", values, settings, now, cooldown=not has_keyword):
        made.append("region_unknown")
    return {"outcome": "region_unknown", "sender_id": sender["id"], "replies": made, "detail": candidate[:100]}
