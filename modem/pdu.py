"""
pdu.py - GSM 03.40 PDU encoding/decoding for SMS: SMS-SUBMIT (send),
SMS-DELIVER (receive), SMS-STATUS-REPORT (delivery reports).

Supports GSM 7-bit (default alphabet + single-shift extension table) and
UCS2, and concatenated (multi-part) messages via the standard 8-bit-
reference UDH. PDU mode is used throughout (AT+CMGF=0) rather than text
mode, since AT+CMGF is a session-wide setting - mixing text-mode reading
with PDU-mode sending isn't practical without fragile mode-toggling across
the several threads that share one control channel.

Known scope limits:
  - the SMSC address is always encoded as length 0, letting the module use
    its own configured/default SMSC rather than us specifying one

Incoming concatenated (multi-part) messages: decode_deliver_pdu() below
decodes each part individually (it has no visibility across AT+CMGL
entries to reassemble anything) - part-buffering, ordering, and merging
into one combined inbox entry is done by manager.py's
_buffer_concat_part()/_deliver_concat_set(), using the concat_ref/
concat_total/concat_seq fields this module extracts from each part's UDH.
"""
from modem.text_codec import GSM7_DEFAULT_ALPHABET

GSM7_CHAR_TO_SEPTET = {ch: i for i, ch in enumerate(GSM7_DEFAULT_ALPHABET)}

# GSM 03.38 single-shift extension table: char -> extension code, sent as
# ESC (septet 0x1B) followed by this code.
GSM7_EXT_TABLE = {
    "\f": 0x0A, "^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F,
    "[": 0x3C, "~": 0x3D, "]": 0x3E, "|": 0x40, "\u20ac": 0x65,  # euro sign
}
GSM7_EXT_REVERSE = {v: k for k, v in GSM7_EXT_TABLE.items()}
GSM7_ESC = 0x1B

# GSM 03.40 semi-octet address alphabet (BCD digits + a couple of specials).
_ADDR_DIGITS = "0123456789*#ab"


class PduError(Exception):
    pass


# ------------------------------------------------------------- GSM7 septets
def is_gsm7_encodable(text):
    return all(ch in GSM7_CHAR_TO_SEPTET or ch in GSM7_EXT_TABLE for ch in text)


def _text_to_septets(text):
    septets = []
    for ch in text:
        if ch in GSM7_CHAR_TO_SEPTET:
            septets.append(GSM7_CHAR_TO_SEPTET[ch])
        elif ch in GSM7_EXT_TABLE:
            septets.append(GSM7_ESC)
            septets.append(GSM7_EXT_TABLE[ch])
        else:
            raise PduError(f"character {ch!r} is not GSM-7 encodable")
    return septets


def _septets_to_text(septets):
    chars = []
    i = 0
    while i < len(septets):
        s = septets[i]
        if s == GSM7_ESC and i + 1 < len(septets):
            chars.append(GSM7_EXT_REVERSE.get(septets[i + 1], ""))
            i += 2
        elif s < len(GSM7_DEFAULT_ALPHABET):
            chars.append(GSM7_DEFAULT_ALPHABET[s])
            i += 1
        else:
            i += 1
    return "".join(chars)


# --------------------------------------------------------------- bit I/O
class _BitWriter:
    """LSB-first bit packer - matches GSM 03.38's septet packing convention."""

    def __init__(self):
        self.out = bytearray()
        self.buffer = 0
        self.nbits = 0

    def write(self, value, width):
        self.buffer |= (value & ((1 << width) - 1)) << self.nbits
        self.nbits += width
        while self.nbits >= 8:
            self.out.append(self.buffer & 0xFF)
            self.buffer >>= 8
            self.nbits -= 8

    def finish(self):
        if self.nbits > 0:
            self.out.append(self.buffer & 0xFF)
            self.nbits = 0
        return bytes(self.out)


class _BitReader:
    """LSB-first bit reader, mirroring _BitWriter's packing exactly."""

    def __init__(self, data):
        self.data = data
        self.byte_pos = 0
        self.bit_pos = 0

    def read(self, width):
        result = 0
        got = 0
        while got < width:
            if self.byte_pos >= len(self.data):
                got = width  # ran out - pad with zero bits rather than raise
                break
            available = 8 - self.bit_pos
            take = min(available, width - got)
            chunk = (self.data[self.byte_pos] >> self.bit_pos) & ((1 << take) - 1)
            result |= chunk << got
            got += take
            self.bit_pos += take
            if self.bit_pos >= 8:
                self.bit_pos = 0
                self.byte_pos += 1
        return result

    def skip(self, width):
        self.read(width)


def _pack_septets(septets, header_bytes=b""):
    """Packs GSM7 septets, with an optional raw UDH prefix, per GSM 03.40's
    fill-bit alignment rule: the header occupies whole *septet slots*, so if
    its bit length isn't a multiple of 7, padding bits are inserted before
    the text septets begin. Returns (packed_bytes, tp_udl) where tp_udl is
    the User Data Length in septets (header slots + text septets)."""
    writer = _BitWriter()
    header_septet_slots = 0
    if header_bytes:
        for b in header_bytes:
            writer.write(b, 8)
        header_bits = len(header_bytes) * 8
        fill_bits = (7 - header_bits % 7) % 7
        if fill_bits:
            writer.write(0, fill_bits)
        header_septet_slots = (header_bits + fill_bits) // 7
    for s in septets:
        writer.write(s, 7)
    return writer.finish(), header_septet_slots + len(septets)


def _unpack_septets(data, num_septets, header_octets=0):
    """Unpacks `num_septets` 7-bit values from `data`, skipping past a
    `header_octets`-byte header (and its fill-bit padding) first if present."""
    reader = _BitReader(data)
    if header_octets:
        header_bits = header_octets * 8
        fill_bits = (7 - header_bits % 7) % 7
        reader.skip(header_bits + fill_bits)
    return [reader.read(7) for _ in range(num_septets)]


# ------------------------------------------------------------- addresses
def encode_address(number):
    """Encodes a destination/originating address. Returns (num_digits,
    type_byte, octets). A leading '+' means international format;
    otherwise encoded as "unknown" type, which is what most networks
    expect for a bare local-format number."""
    number = number.strip()
    if number.startswith("+"):
        number = number[1:]
        type_byte = 0x91  # international, ISDN/telephone numbering plan
    else:
        type_byte = 0x81  # unknown type, ISDN/telephone numbering plan
    digits = "".join(c for c in number if c in "0123456789*#ab")
    num_digits = len(digits)
    padded = digits + ("F" if len(digits) % 2 else "")
    octets = bytearray()
    for i in range(0, len(padded), 2):
        low = _addr_digit_value(padded[i])
        high = _addr_digit_value(padded[i + 1])
        octets.append((high << 4) | low)
    return num_digits, type_byte, bytes(octets)


def decode_address(num_digits, type_byte, octets):
    digits = []
    for byte in octets:
        low = byte & 0x0F
        high = (byte >> 4) & 0x0F
        digits.append(_addr_digit_char(low))
        digits.append(_addr_digit_char(high))
    number = "".join(digits)[:num_digits]
    ton = (type_byte >> 4) & 0x07
    return ("+" + number) if ton == 1 else number


def _addr_digit_value(ch):
    if ch == "F":
        return 0xF
    return _ADDR_DIGITS.index(ch)


def _addr_digit_char(nibble):
    if nibble < len(_ADDR_DIGITS):
        return _ADDR_DIGITS[nibble]
    return ""  # 0xE/0xF - padding/unused


# ------------------------------------------------------------- timestamps
def _decode_bcd_pair(byte):
    """2 decimal digits, nibble-swapped (low nibble = first digit)."""
    return (byte & 0x0F) * 10 + ((byte >> 4) & 0x0F)


def _encode_bcd_pair(value):
    tens, units = divmod(value, 10)
    return (units << 4) | tens


def decode_timestamp(data7):
    """Decodes a 7-octet TP-SCTS/TP-DT timestamp into (epoch_utc, raw_str),
    matching the same semantics as merge.py's text-mode timestamp handling
    (quarter-hour timezone offset from GMT)."""
    import calendar
    import time as _time

    yy = _decode_bcd_pair(data7[0])
    mm = _decode_bcd_pair(data7[1])
    dd = _decode_bcd_pair(data7[2])
    hh = _decode_bcd_pair(data7[3])
    mi = _decode_bcd_pair(data7[4])
    ss = _decode_bcd_pair(data7[5])

    tz_byte = data7[6]
    tz_low = tz_byte & 0x0F
    tz_high = (tz_byte >> 4) & 0x0F
    sign = -1 if (tz_low & 0x08) else 1
    tz_tens = tz_low & 0x07
    tz_quarters = sign * (tz_tens * 10 + tz_high)

    year = 2000 + yy
    struct = _time.struct_time((year, mm, dd, hh, mi, ss, 0, 0, -1))
    epoch_as_if_utc = calendar.timegm(struct)
    epoch = epoch_as_if_utc - tz_quarters * 15 * 60
    raw = f"{yy:02d}/{mm:02d}/{dd:02d},{hh:02d}:{mi:02d}:{ss:02d}{'+' if tz_quarters >= 0 else '-'}{abs(tz_quarters):02d}"
    return epoch, raw


def encode_timestamp(epoch, tz_quarters=0):
    """Encodes a UTC epoch (plus a quarter-hour tz offset to *report*, not
    to convert by) into a 7-octet TP-SCTS-style field. Only used internally
    by our own tests to build known-good round-trip fixtures - production
    code only ever decodes timestamps (we never send one in a SUBMIT PDU)."""
    import time as _time
    t = _time.gmtime(epoch + tz_quarters * 15 * 60)
    out = bytearray()
    out.append(_encode_bcd_pair(t.tm_year % 100))
    out.append(_encode_bcd_pair(t.tm_mon))
    out.append(_encode_bcd_pair(t.tm_mday))
    out.append(_encode_bcd_pair(t.tm_hour))
    out.append(_encode_bcd_pair(t.tm_min))
    out.append(_encode_bcd_pair(t.tm_sec))
    sign_bit = 0x08 if tz_quarters < 0 else 0x00
    tz_tens, tz_units = divmod(abs(tz_quarters), 10)
    out.append((tz_units << 4) | tz_tens | sign_bit)
    return bytes(out)


# ------------------------------------------------------------- hex helpers
def _hex(data):
    return data.hex().upper()


# ---------------------------------------------------------------- SUBMIT
MAX_GSM7_SINGLE = 160
MAX_GSM7_MULTI = 153      # per part, once UDH is present
MAX_UCS2_SINGLE = 70
MAX_UCS2_MULTI = 67


def encode_submit_pdu(recipient, text, reference=0, request_status_report=True):
    """Encodes `text` to one or more SMS-SUBMIT PDUs (a list of hex strings,
    one per segment). `reference` is the concatenation reference used across
    all parts of a multi-part message (0-255) - unrelated to the TP-MR the
    module assigns per-part (that comes back from the AT+CMGS response).

    Returns a list of (pdu_hex, tpdu_length) tuples in send order."""
    use_gsm7 = is_gsm7_encodable(text)
    parts = _split_text(text, use_gsm7)
    multipart = len(parts) > 1

    results = []
    for idx, part_text in enumerate(parts):
        udh = None
        if multipart:
            udh_content = bytes([0x00, 0x03, reference & 0xFF, len(parts), idx + 1])
            udh = bytes([len(udh_content)]) + udh_content  # prepend UDHL
        pdu_hex, tpdu_len = _encode_single_submit(
            recipient, part_text, use_gsm7, udh, request_status_report,
        )
        results.append((pdu_hex, tpdu_len))
    return results


def _split_text(text, use_gsm7):
    single_limit = MAX_GSM7_SINGLE if use_gsm7 else MAX_UCS2_SINGLE
    if len(text) <= single_limit:
        return [text]
    multi_limit = MAX_GSM7_MULTI if use_gsm7 else MAX_UCS2_MULTI
    return [text[i:i + multi_limit] for i in range(0, len(text), multi_limit)]


def _encode_single_submit(recipient, text, use_gsm7, udh, request_status_report):
    num_digits, da_type, da_octets = encode_address(recipient)

    pdu_type = 0x01  # TP-MTI = SMS-SUBMIT
    if request_status_report:
        pdu_type |= 0x20  # TP-SRR
    if udh:
        pdu_type |= 0x40  # TP-UDHI

    out = bytearray()
    out.append(0x00)              # SCA length 0 - use the module's own default SMSC
    out.append(pdu_type)
    out.append(0x00)              # TP-MR - let the module assign/report the real one
    out.append(num_digits)
    out.append(da_type)
    out.extend(da_octets)
    out.append(0x00)              # TP-PID: normal
    out.append(0x08 if not use_gsm7 else 0x00)  # TP-DCS: UCS2 or GSM7 default
    # TP-VPF left as "not present" (bits already 0 in pdu_type) - no TP-VP field

    if use_gsm7:
        septets = _text_to_septets(text)
        packed, udl = _pack_septets(septets, header_bytes=(udh or b""))
        out.append(udl)
        out.extend(packed)
    else:
        ud = (udh or b"") + text.encode("utf-16-be")
        out.append(len(ud))
        out.extend(ud)

    # TPDU length for AT+CMGS excludes the SCA octet (the leading 00 above)
    tpdu_length = len(out) - 1
    return _hex(bytes(out)), tpdu_length


# --------------------------------------------------------------- DELIVER
def decode_deliver_pdu(pdu_hex):
    """Decodes an incoming SMS-DELIVER PDU. Returns a dict: sender,
    received_at (epoch or None), raw_timestamp, text, and (if the message
    is one part of a concatenated set) concat_ref/concat_total/concat_seq.
    Reassembly across parts is manager.py's job (see module docstring) -
    this function only ever sees one PDU at a time."""
    data = bytes.fromhex(pdu_hex)
    pos = 0

    sca_len = data[pos]; pos += 1
    pos += sca_len  # skip the SMSC address entirely - we don't need it

    pdu_type = data[pos]; pos += 1
    has_udh = bool(pdu_type & 0x40)

    oa_digits = data[pos]; pos += 1
    oa_type = data[pos]; pos += 1
    oa_octets_len = (oa_digits + 1) // 2
    oa_octets = data[pos:pos + oa_octets_len]; pos += oa_octets_len
    sender = decode_address(oa_digits, oa_type, oa_octets)

    pos += 1  # TP-PID
    dcs = data[pos]; pos += 1
    use_ucs2 = bool(dcs & 0x08)

    ts_bytes = data[pos:pos + 7]; pos += 7
    received_at, raw_timestamp = decode_timestamp(ts_bytes)

    udl = data[pos]; pos += 1
    ud = data[pos:]

    concat_ref = concat_total = concat_seq = None
    header_octets = 0
    if has_udh:
        udhl = ud[0]
        header = ud[1:1 + udhl]
        header_octets = 1 + udhl
        concat_ref, concat_total, concat_seq = _parse_concat_udh(header)

    if use_ucs2:
        text_bytes = ud[header_octets:]
        text = text_bytes.decode("utf-16-be", errors="replace")
    else:
        num_septets = udl - (_header_septet_slots(header_octets) if has_udh else 0)
        septets = _unpack_septets(ud, max(num_septets, 0), header_octets=header_octets)
        text = _septets_to_text(septets)

    return {
        "sender": sender,
        "received_at": received_at,
        "raw_timestamp": raw_timestamp,
        "text": text,
        "concat_ref": concat_ref,
        "concat_total": concat_total,
        "concat_seq": concat_seq,
    }


def _header_septet_slots(header_octets):
    header_bits = header_octets * 8
    fill_bits = (7 - header_bits % 7) % 7
    return (header_bits + fill_bits) // 7


def _parse_concat_udh(header_bytes):
    """Looks for IEI 0x00 (concatenated SM, 8-bit reference) in the UDH.
    Returns (ref, total, seq) or (None, None, None) if not present."""
    i = 0
    while i + 1 < len(header_bytes):
        iei = header_bytes[i]
        iedl = header_bytes[i + 1]
        ie_data = header_bytes[i + 2:i + 2 + iedl]
        if iei == 0x00 and len(ie_data) >= 3:
            return ie_data[0], ie_data[1], ie_data[2]
        i += 2 + iedl
    return None, None, None


# ------------------------------------------------------------- STATUS REPORT
# TP-ST status codes we actually care about distinguishing (3GPP 23.040 9.2.3.15)
STATUS_DELIVERED = 0x00
# 0x01-0x1F: still in progress in some way (forwarded, replaced, etc.) -
# treat as "still pending" rather than success or failure
# 0x20+: failure/error of some kind


def decode_status_report_pdu(pdu_hex):
    """Decodes an incoming SMS-STATUS-REPORT PDU. Returns a dict: mr
    (the TP-MR this report is for - matches what AT+CMGS returned when we
    sent the original message), recipient, status_code, and a simplified
    status: 'delivered', 'pending', or 'failed'."""
    data = bytes.fromhex(pdu_hex)
    pos = 0

    sca_len = data[pos]; pos += 1
    pos += sca_len

    pos += 1  # PDU type (SMS-STATUS-REPORT) - fixed shape, nothing to branch on here

    mr = data[pos]; pos += 1

    ra_digits = data[pos]; pos += 1
    ra_type = data[pos]; pos += 1
    ra_octets_len = (ra_digits + 1) // 2
    ra_octets = data[pos:pos + ra_octets_len]; pos += ra_octets_len
    recipient = decode_address(ra_digits, ra_type, ra_octets)

    pos += 7  # SC timestamp - when we submitted it; not what we need here
    dt_bytes = data[pos:pos + 7]; pos += 7
    discharge_at, discharge_raw = decode_timestamp(dt_bytes)

    status_code = data[pos]; pos += 1
    if status_code == STATUS_DELIVERED:
        status = "delivered"
    elif status_code < 0x20:
        status = "pending"
    else:
        status = "failed"

    return {
        "mr": mr,
        "recipient": recipient,
        "status_code": status_code,
        "status": status,
        "discharge_at": discharge_at,
        "discharge_raw": discharge_raw,
    }
