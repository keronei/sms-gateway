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
from modem.serial_at import ATError

CMGS_REF_RE = re.compile(r'^\+CMGS:\s*(\d+)$')
CMGL_PDU_HEADER_RE_FIELDS = 4  # <index>,<stat>,<alpha>,<length>

# AT+CNMI's first parameter (<mode>, notification routing mode) - per the
# Huawei MU509 AT Command Interface Spec section 6.3.3, mode=2 ("report
# notification and state report to the TE directly... reserved, not
# supported currently") is NOT actually supported on this module, despite
# AT+CNMI=? advertising it as a valid value. That's almost certainly what
# was behind the original "+CMS ERROR: 303" on ds=1 - not something wrong
# with ds itself. mode=1 is the module's own documented example for this
# exact use case (store incoming via +CMTI, direct delivery reports via
# +CDS): "AT+CNMI=1,1,0,1,0" (spec section 6.3.4).
CNMI_MODE = 1

# AT+CNMI fourth parameter (ds - delivery report reporting mode), tried in
# this order. ds=1 (direct +CDS delivery, paired with CNMI_MODE=1 above)
# is what the module's own documented example uses and should simply
# work; ds=0 (no delivery reports at all) is the last-resort fallback if
# ds=1 is still rejected for some other reason - it costs real
# functionality (sent messages can never transition to delivered/failed),
# so it's only used when ds=1 fails.
#
# Deliberately NOT including ds=2 here: per the same spec (sections 6.3.3,
# 6.4.3, 6.6.2), "SR" (status report) storage is "reserved, not supported
# currently" on this module. ds=2 would make +CDSI fire, but there is no
# way to ever read the report back - it's simply lost. That's worse than
# ds=0 (silent instead of visibly absent), so it's excluded from the
# default probe order. It's still usable via an explicit ds_order if a
# future firmware revision turns out to support it.
CNMI_DS_FALLBACK = (1, 0)


def configure(channel, timeout=10, ds_order=CNMI_DS_FALLBACK):
    """PDU mode + store-on-SIM-and-notify for new messages. Probes
    ds_order in turn for the delivery-report (status report) reporting
    mode and keeps the first one the module actually accepts.

    Returns the ds value that succeeded (an int from ds_order).
    Raises the last ATError/ATTimeout if every value in ds_order is
    rejected (AT+CMGF failing is always fatal and raised immediately,
    before any ds is attempted)."""
    channel.send("AT+CMGF=0", timeout=timeout)
    last_err = None
    for ds in ds_order:
        try:
            channel.send(f"AT+CNMI={CNMI_MODE},1,0,{ds},0", timeout=timeout)
            return ds
        except ATError as e:
            last_err = e
            continue
    raise last_err


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
