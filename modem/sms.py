"""
sms.py - text-mode SMS operations on top of an already-open ATChannel.

Deliberately uses AT text mode (AT+CMGF=1) rather than PDU mode: it covers
list/read/delete/send without needing a GSM-7/UCS2/UDH codec, which is the
right tradeoff for the inbox + simple-reply use case. Sending long
(multi-segment) or delivery-report-tracked messages for bulk campaign
dispatch is expected to move to PDU mode in a later milestone - that's a
separate concern from this module.

Knows nothing about the database - manager.py is responsible for persisting
what these functions return.
"""
import re
import csv
import io
import time
import calendar

from modem.serial_at import ATError, ATTimeout
from modem import text_codec

CMGS_REF_RE = re.compile(r'^\+CMGS:\s*(\d+)$')
TIMESTAMP_RE = re.compile(r"^\d{2}/\d{2}/\d{2},\d{2}:\d{2}:\d{2}[+-]\d{1,2}$")


def configure(channel, timeout=10):
    """Text mode + store-on-SIM-and-notify. Call once after the control
    channel is (re)opened."""
    channel.send("AT+CMGF=1", timeout=timeout)
    channel.send("AT+CNMI=2,1,0,0,0", timeout=timeout)


def list_messages(channel, timeout=15):
    """Returns a list of dicts: {sim_index, status, sender, raw_timestamp,
    received_at (epoch float or None), body}."""
    resp = channel.send('AT+CMGL="ALL"', timeout=timeout)
    return _parse_cmgl(resp.lines)


def delete_message(channel, sim_index, timeout=10):
    channel.send(f"AT+CMGD={sim_index}", timeout=timeout)


def send_text(channel, phone, text, prompt_timeout=10, send_timeout=30):
    """Sends a single text-mode SMS. Returns the message reference number
    (int) if the modem reports one, else None. Raises ATError/ATTimeout."""
    channel.send_expect_prompt(f'AT+CMGS="{phone}"', timeout=prompt_timeout)
    resp = channel.send_payload(text, ctrl_z=True, timeout=send_timeout)
    for line in resp.lines:
        m = CMGS_REF_RE.match(line.strip())
        if m:
            return int(m.group(1))
    return None


# ------------------------------------------------------------------ parsing
def _parse_cmgl_header(line):
    """+CMGL: <index>,<stat>,<oa>,[<alpha>],<scts>  - <alpha> is normally
    omitted (giving the "...,," most examples show) but some carriers
    populate it with a sender name alongside a shortcode/number, which the
    previous rigid regex here didn't account for and would silently mis-
    parse. Using csv.reader to split fields properly handles both: it
    respects quoting (so the timestamp's internal comma doesn't split it
    into two fields) and naturally gives an empty string for omitted
    fields (",,") without needing a fixed field count."""
    if not line.startswith("+CMGL:"):
        return None
    rest = line[len("+CMGL:"):].strip()
    try:
        row = next(csv.reader(io.StringIO(rest), skipinitialspace=True))
    except (csv.Error, StopIteration):
        return None
    if len(row) < 3:
        return None
    try:
        sim_index = int(row[0].strip())
    except ValueError:
        return None

    status = row[1].strip() if len(row) > 1 else ""
    sender = row[2].strip() if len(row) > 2 else ""
    alpha = row[3].strip() if len(row) > 3 else ""
    raw_timestamp = ""
    for candidate in row[4:] + ([alpha] if alpha else []):
        if TIMESTAMP_RE.match(candidate.strip()):
            raw_timestamp = candidate.strip()
            break

    # if the alpha tag is populated (a friendly sender name alongside a
    # shortcode/number) and isn't just a duplicate of the sender field,
    # fold it into a "Name <number>" style display
    if alpha and alpha != sender and not TIMESTAMP_RE.match(alpha):
        sender_display = f"{alpha} <{sender}>" if sender else alpha
    else:
        sender_display = sender

    return {
        "sim_index": sim_index,
        "status": status,
        "sender": text_codec.decode_possible_hex(sender_display),
        "raw_timestamp": raw_timestamp,
    }


def _parse_cmgl(lines):
    """Header lines (see _parse_cmgl_header) followed by the body line(s)
    until the next header. Message bodies could in principle contain a line
    starting with '+CMGL:' themselves, which would confuse this - an
    accepted, extremely unlikely edge case for a text-mode SMS inbox."""
    records = []
    current = None
    body_lines = []

    def _flush():
        if current is not None:
            body = "\n".join(body_lines).strip()
            current["body"] = text_codec.decode_possible_hex(body)
            current["received_at"] = _parse_timestamp(current["raw_timestamp"])
            records.append(current)

    for line in lines:
        header = _parse_cmgl_header(line.strip())
        if header:
            _flush()
            current = header
            body_lines = []
        elif current is not None:
            body_lines.append(line)
    _flush()
    return records


def _parse_timestamp(raw):
    """'yy/MM/dd,hh:mm:ss+zz' where zz is the timezone offset in *quarter
    hours* from GMT (3GPP TS 27.005). Falls back to None (caller should use
    time.time() as the received_at) if parsing fails - the raw string is
    always preserved regardless."""
    try:
        m = re.match(r"(\d{2})/(\d{2})/(\d{2}),(\d{2}):(\d{2}):(\d{2})([+-]\d{1,2})", raw or "")
        if not m:
            return None
        yy, MM, dd, hh, mm, ss, tz_quarters = m.groups()
        year = 2000 + int(yy)
        tz_minutes = int(tz_quarters) * 15
        dt = time.struct_time((year, int(MM), int(dd), int(hh), int(mm), int(ss), 0, 0, -1))
        # the fields are "local to the handset"; treat them as UTC first via
        # calendar.timegm, then shift by the reported offset to get true UTC.
        epoch_as_if_utc = calendar.timegm(dt)
        return epoch_as_if_utc - tz_minutes * 60
    except (ValueError, TypeError):
        return None
