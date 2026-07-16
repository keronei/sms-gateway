"""
ussd.py - USSD session handling on top of an already-open ATChannel.

AT+CUSD's own OK response only confirms the request was *accepted* - the
network's actual reply arrives later as an unsolicited "+CUSD: <m>,<str>,<dcs>"
line (this is true whether it's the answer to a code we dialed, or a
mid-session menu reply). UssdWaiter bridges that: manager.py's URC callback
feeds every line through on_urc_line(), and whichever thread sent the
request calls wait_for_reply() to block (with a timeout) until it shows up -
the same way you'd watch a USSD popup appear on a phone.

Session semantics (3GPP TS 27.007 AT+CUSD): sending an initial code and
replying mid-session use the *identical* AT+CUSD=1,"<text>",15 command - the
network just knows which session it belongs to. AT+CUSD=2 ends a session.

<m> (session state) values: 0 = no further action needed (session over),
1 = further action required (network expects a reply), 2 = terminated by
network, 4 = operation not supported, 5 = network timeout.
"""
import re
import threading

CUSD_RE = re.compile(r'^\+CUSD:\s*(\d)(?:\s*,\s*"((?:[^"\\]|\\.)*)"(?:\s*,\s*(\d+))?)?\s*$')


class UssdWaiter:
    def __init__(self):
        self._event = threading.Event()
        self._result = None

    def on_urc_line(self, line):
        """Returns True if this line was a +CUSD: reply and has been consumed."""
        m = CUSD_RE.match(line.strip())
        if not m:
            return False
        state_str, raw_text, dcs_str = m.groups()
        dcs = int(dcs_str) if dcs_str is not None else 15
        text = decode_ussd_text(dcs, raw_text or "")
        self._result = {
            "session_state": int(state_str),
            "text": text,
            "raw_text": raw_text,
            "dcs": dcs,
        }
        self._event.set()
        return True

    def wait_for_reply(self, timeout=30):
        self._event.clear()
        if self._event.wait(timeout):
            return self._result
        return None


def send(channel, text, timeout=10):
    """Dial an initial USSD code, or reply mid-session - identical AT syntax
    either way. Our codes are always plain ASCII (*144#, menu digits, ...),
    so dcs is always 15 (GSM7/plain text) on the way out."""
    channel.send(f'AT+CUSD=1,"{_escape(text)}",15', timeout=timeout)


def end_session(channel, timeout=10):
    channel.send("AT+CUSD=2", timeout=timeout)


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ------------------------------------------------------------------ decode
def decode_ussd_text(dcs, text):
    """Best-effort decode of a +CUSD response string. dcs=15 (and 0) are
    plain text and returned as-is. UCS2 (commonly dcs=72, sometimes other
    values depending on chipset/network quirks) is a hex string, 4 hex
    digits per UTF-16 code unit. Different networks/modems are known to be
    inconsistent about the exact dcs value here, so as a safety net we also
    try to detect "this really looks like UCS2 hex" even when dcs doesn't
    match the expected value, rather than showing raw hex to the user."""
    if dcs in (0, 15):
        return text
    if _looks_like_ucs2_hex(text):
        decoded = _try_decode_ucs2(text)
        if decoded is not None:
            return decoded
    return text


def _looks_like_ucs2_hex(text):
    return bool(text) and len(text) % 4 == 0 and all(c in "0123456789abcdefABCDEF" for c in text)


def _try_decode_ucs2(text):
    try:
        raw = bytes.fromhex(text)
        decoded = raw.decode("utf-16-be")
        # sanity check: reject if it decoded to mostly control/non-printable
        # junk, which suggests this wasn't actually UCS2 after all
        printable = sum(1 for ch in decoded if ch.isprintable())
        if decoded and printable / len(decoded) > 0.8:
            return decoded
        return None
    except (ValueError, UnicodeDecodeError):
        return None
