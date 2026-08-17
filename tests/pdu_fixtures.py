"""
pdu_fixtures.py - test-only helpers for building SMS-DELIVER PDU hex
strings.

modem/pdu.py deliberately has no DELIVER *encoder* in production code -
we only ever receive those, never construct them - so tests that need a
realistic incoming PDU (single-part or one part of a concatenated set)
build one here. Field-for-field this mirrors what
modem/pdu.py::decode_deliver_pdu() parses, so a round trip through
build_deliver_pdu() -> decode_deliver_pdu() is a meaningful check that
both sides agree on the wire format - it isn't just feeding decode_*
its own assumptions back at itself, since the byte layout below is
written independently by hand against GSM 03.40, the same way a real
network-originated PDU would be laid out.
"""
import time as _time

from modem import pdu


def build_deliver_pdu(sender, text, concat=None, epoch=None, tz_quarters=0):
    """Builds a single SMS-DELIVER PDU hex string.

    concat: optional (ref, total, seq) tuple. When given, adds the
    standard 8-bit-reference concatenation UDH (IEI 0x00) and sets
    TP-UDHI, the same shape modem/pdu.py::_parse_concat_udh() expects.
    """
    if epoch is None:
        epoch = int(_time.time())

    num_digits, oa_type, oa_octets = pdu.encode_address(sender)
    use_gsm7 = pdu.is_gsm7_encodable(text)

    udh = None
    if concat is not None:
        ref, total, seq = concat
        udh_content = bytes([0x00, 0x03, ref & 0xFF, total & 0xFF, seq & 0xFF])
        udh = bytes([len(udh_content)]) + udh_content

    pdu_type = 0x00  # TP-MTI = SMS-DELIVER, TP-MMS not set (irrelevant here)
    if udh:
        pdu_type |= 0x40  # TP-UDHI

    out = bytearray()
    out.append(0x00)  # SMSC address length 0 - module fills in its own
    out.append(pdu_type)
    out.append(num_digits)
    out.append(oa_type)
    out.extend(oa_octets)
    out.append(0x00)  # TP-PID: normal
    out.append(0x00 if use_gsm7 else 0x08)  # TP-DCS: GSM7 default / UCS2
    out.extend(pdu.encode_timestamp(epoch, tz_quarters=tz_quarters))

    if use_gsm7:
        septets = pdu._text_to_septets(text)
        packed, udl = pdu._pack_septets(septets, header_bytes=(udh or b""))
        out.append(udl)
        out.extend(packed)
    else:
        ud = (udh or b"") + text.encode("utf-16-be")
        out.append(len(ud))
        out.extend(ud)

    return bytes(out).hex().upper()


def cmgl_lines(entries):
    """Builds the raw response-line list AT+CMGL=4 would return for a set
    of (sim_index, stat, pdu_hex) entries - i.e. what sms._parse_cmgl_pdu
    consumes. `stat` defaults to 1 ("REC READ") when omitted per entry."""
    lines = []
    for entry in entries:
        if len(entry) == 2:
            sim_index, pdu_hex = entry
            stat = 1
        else:
            sim_index, stat, pdu_hex = entry
        lines.append(f"+CMGL: {sim_index},{stat},,{len(pdu_hex) // 2}")
        lines.append(pdu_hex)
    return lines
