"""
parser.py - turns a forwarded confirmation SMS into structured payments.

The pattern is a regex with NAMED groups, stored in settings so a changed
SMS wording never needs a code change. Required groups: amount, code,
account, date, time. Optional: paybill, account_name.

The default matches the KCB paybill confirmation, e.g.
  "Ksh 200.00 sent to KCB Pay Bill 522522 for account 133***6248 MINISTRY
   OF REPENTANCE AND HOLINESS has been received on 24/05/2026 at 11:27 AM.
   M-PESA ref UEOQP5BEC6. To reverse this transaction, SMS this message to 16120."

Matching is a SEARCH, not an anchored match, so "Fwd:" prefixes, line
wraps added by the forwarding phone, or several confirmations forwarded in
one message all still work (every match is returned).
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

DEFAULT_PATTERN = (
    r"(?:Ksh|KES)\.?\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)"
    r"\s+sent\s+to\s+(?:.{0,60}?)\s*Pay\s*Bill\s*(?:No\.?|number)?\s*(?P<paybill>\d{4,8})"
    r"\s+for\s+account\s+(?P<account>[A-Za-z0-9*#_\-]+)"
    r"(?:\s+(?P<account_name>.{1,80}?))?"
    r"\s+has\s+been\s+received\s+on\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})"
    r"\s+at\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?)"
    r"\.?\s*M-?PESA\s+ref(?:erence)?\.?:?\s*(?P<code>[A-Za-z0-9]{8,12})"
)
REQUIRED_GROUPS = {"amount", "code", "account", "date", "time"}
MAX_BODY_CHARS = 3000   # an SMS tops out far below this; bounds regex work on hostile input


class PatternError(ValueError):
    pass


class Payment:
    """One payment found in a message. `error` is set (and the numeric
    fields may be None) when the text matched but a value was unusable -
    the caller records that as a rejection instead of silently dropping it."""
    __slots__ = ("amount_cents", "code", "account_ref", "paybill", "account_name", "paid_at", "error", "text")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return f"Payment({self.code} {self.amount_cents} {self.account_ref} {self.paid_at} err={self.error})"


def compile_pattern(pattern):
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error as e:
        raise PatternError(f"Invalid regular expression: {e}")
    missing = REQUIRED_GROUPS - set(rx.groupindex)
    if missing:
        raise PatternError("Pattern is missing required named group(s): " + ", ".join(sorted(missing)))
    return rx


def parse_amount_cents(text):
    try:
        d = Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    cents = int((d * 100).to_integral_value())
    return cents if cents > 0 else None


def parse_paid_at(date_s, time_s):
    """'24/05/2026' + '11:27 AM' -> '2026-05-24 11:27:00' (naive local time)."""
    try:
        day, month, year = (int(x) for x in date_s.split("/"))
        if year < 100:
            year += 2000
        t = time_s.strip().upper().replace(".", "")
        ampm = None
        m = re.search(r"\s*([AP])M?$", t)
        if m:
            ampm = m.group(1)
            t = t[:m.start()]
        parts = [int(x) for x in t.split(":")]
        hour, minute = parts[0], parts[1]
        second = parts[2] if len(parts) > 2 else 0
        if ampm:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if ampm == "P" else 0)
        return datetime(year, month, day, hour, minute, second).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None


def parse_payments(body, pattern=DEFAULT_PATTERN):
    """Returns a list of Payment (possibly empty), de-duplicated by code
    within the message. Raises PatternError only for a broken pattern."""
    rx = compile_pattern(pattern)
    text = re.sub(r"\s+", " ", (body or "")[:MAX_BODY_CHARS]).strip()
    out, seen = [], set()
    for m in rx.finditer(text):
        g = m.groupdict()
        code = (g.get("code") or "").upper()
        if code in seen:
            continue
        seen.add(code)
        p = Payment(
            code=code,
            account_ref=(g.get("account") or "").strip().upper(),
            paybill=(g.get("paybill") or "").strip() or None,
            account_name=(g.get("account_name") or "").strip() or None,
            text=m.group(0),
        )
        p.amount_cents = parse_amount_cents(g.get("amount") or "")
        p.paid_at = parse_paid_at(g.get("date") or "", g.get("time") or "")
        if p.amount_cents is None:
            p.error = "amount could not be read"
        elif p.paid_at is None:
            p.error = "date/time could not be read"
        out.append(p)
    return out
