"""
sms.py - PDU-mode SMS operations on top of an already-open ATChannel.

Uses AT+CMGF=0 (PDU mode) rather than text mode: this is what enables
multi-segment (concatenated) sending and delivery-report (+CDS)
correlation, neither of which text mode supports. AT+CMGF is a session-
wide setting, so mixing text-mode reading with PDU-mode sending isn't
practical - everything (list/read/delete/send) goes through PDU mode here.

Knows nothing about the database - manager.py is responsible for persisting
what these functions return. The external shape of list_messages()'s
results is unchanged from the old text-mode version (sim_index, status,
sender, raw_timestamp, received_at, body), so callers didn't need to change.
"""
import re
import csv
import io

from modem import pdu
from modem import text_codec

CMGS_REF_RE = re.compile(r'^\+CMGS:\s*(\d+)$')
CMGL_PDU_HEADER_RE_FIELDS = 4  # <index>,<stat>,<alpha>,<length>


def configure(channel, timeout=10):
    """PDU mode + store-on-SIM-and-notify for new messages, with direct
    URC delivery for delivery (status) reports (the ds=1 in CNMI)."""
    channel.send("AT+CMGF=0", timeout=timeout)
    channel.send("AT+CNMI=2,1,0,1,0", timeout=timeout)


def list_messages(channel, timeout=15):
    """Returns a list of dicts: {sim_index, status, sender, raw_timestamp,
    received_at (epoch float or None), body}."""
    resp = channel.send("AT+CMGL=4", timeout=timeout)  # 4 = ALL messages, PDU mode
    return _parse_cmgl_pdu(resp.lines)


def delete_message(channel, sim_index, timeout=10):
    channel.send(f"AT+CMGD={sim_index}", timeout=timeout)


def send_message(channel, phone, text, reference=0, request_status_report=True,
                  prompt_timeout=10, send_timeout=30):
    """Sends `text` as one or more SMS-SUBMIT PDUs, auto-segmenting into
    multiple parts if needed. Returns a list of (part_seq, part_total, mr)
    tuples, one per segment actually sent - mr is the message reference the
    modem reports for that segment, used to correlate a later +CDS:
    delivery report (matched against (mr, recipient))."""
    parts = pdu.encode_submit_pdu(phone, text, reference=reference,
                                   request_status_report=request_status_report)
    total = len(parts)
    results = []
    for seq, (pdu_hex, tpdu_len) in enumerate(parts, start=1):
        channel.send_expect_prompt(f"AT+CMGS={tpdu_len}", timeout=prompt_timeout)
        resp = channel.send_payload(pdu_hex, ctrl_z=True, timeout=send_timeout)
        mr = None
        for line in resp.lines:
            m = CMGS_REF_RE.match(line.strip())
            if m:
                mr = int(m.group(1))
                break
        results.append((seq, total, mr))
    return results


# ------------------------------------------------------------------ parsing
def _parse_cmgl_pdu_header(line):
    """+CMGL: <index>,<stat>,[<alpha>],<length> - <alpha> is normally empty
    but, as with text mode, some carriers/phonebook matches can populate it,
    so this uses the same csv-based field splitting as the old text-mode
    parser rather than a rigid fixed-format regex."""
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
        stat = int(row[1].strip())
    except ValueError:
        return None
    return {"sim_index": sim_index, "status": str(stat)}


def _parse_cmgl_pdu(lines):
    """Header lines (see _parse_cmgl_pdu_header) followed by exactly one
    PDU-hex line each. Anything that fails to decode is skipped rather than
    aborting the whole drain - one malformed entry shouldn't lose the rest
    of the inbox."""
    records = []
    i = 0
    while i < len(lines):
        header = _parse_cmgl_pdu_header(lines[i].strip())
        if header and i + 1 < len(lines):
            pdu_hex = lines[i + 1].strip()
            try:
                decoded = pdu.decode_deliver_pdu(pdu_hex)
            except Exception:
                i += 2
                continue
            records.append({
                "sim_index": header["sim_index"],
                "status": header["status"],
                "sender": text_codec.decode_possible_hex(decoded["sender"]),
                "raw_timestamp": decoded["raw_timestamp"],
                "received_at": decoded["received_at"],
                "body": decoded["text"],
            })
            i += 2
        else:
            i += 1
    return records
