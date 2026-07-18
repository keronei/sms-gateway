"""
text_codec.py - best-effort decoding of AT response text that might actually
be encoded bytes rather than already-readable text.

Both SMS bodies (AT+CMGL/CMGR in text mode) and USSD replies (+CUSD:) can
come back in one of several shapes instead of plain text, depending on
firmware/carrier quirks:

  - hex-encoded UCS2: 4 hex digits per UTF-16BE code unit (e.g. "0048" = 'H')
  - hex-encoded raw 8-bit/ASCII bytes: 2 hex digits per byte
  - raw packed 7-bit GSM septets (GSM 03.38), sent directly as bytes over
    the wire rather than hex-represented at all - this shows up as
    seemingly-random extended-Latin/control characters once the transport
    layer correctly preserves the raw bytes (see serial_at.py's latin-1
    decode) instead of mangling them

We can't always know which applies just from a dcs/status flag (and
different firmware/carriers are inconsistent about it), so this module
detects each shape and tries the decode, keeping whichever produces
mostly-printable text. Relies on the serial transport layer preserving raw
byte values losslessly - otherwise the bytes needed here would already be
destroyed before reaching this code.
"""

PRINTABLE_RATIO_THRESHOLD = 0.8

# GSM 03.38 default alphabet, indexed 0-127 by septet value.
GSM7_DEFAULT_ALPHABET = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
assert len(GSM7_DEFAULT_ALPHABET) == 128


def looks_like_hex(text):
    return bool(text) and len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text)


def try_decode_ucs2_hex(text):
    if not text or len(text) % 4 != 0:
        return None
    try:
        raw = bytes.fromhex(text)
        decoded = raw.decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if _mostly_ascii_printable(decoded) else None


def try_decode_ascii_hex(text):
    """2 hex digits per byte - covers plain ASCII and UTF-8-encoded text
    that got hex-represented (e.g. a promotional SMS with a URL)."""
    if not text or len(text) % 2 != 0:
        return None
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    for encoding in ("utf-8", "latin-1"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _mostly_ascii_printable(decoded):
            return decoded
    return None


def try_decode_packed_gsm7(text):
    """Recovers the original bytes (via latin-1, which is lossless for
    whatever serial_at.py handed us) and unpacks them as GSM 7-bit default-
    alphabet septets. This is for firmware that sends already-packed septet
    bytes directly, rather than hex-representing them - a different quirk
    than the hex cases above, and the one that matches "extended-Latin/box
    characters" rather than "looks like hex digits"."""
    if not text:
        return None
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return None
    septets = _unpack_septets(raw)
    if not septets:
        return None
    # a lone trailing septet 0 is a common padding artifact (leftover zero
    # bits when the septet count isn't a clean multiple of 8) rather than a
    # real '@' character - drop it before mapping to characters
    if len(septets) > 1 and septets[-1] == 0:
        septets = septets[:-1]
    chars = [GSM7_DEFAULT_ALPHABET[s] for s in septets if s < len(GSM7_DEFAULT_ALPHABET)]
    decoded = "".join(chars)
    return decoded if _mostly_ascii_printable(decoded) else None


def _unpack_septets(raw_bytes):
    """Standard GSM 03.38 unpacking: treat the byte stream as a continuous
    LSB-first bit stream and pull out 7 bits at a time."""
    septets = []
    buffer = 0
    bits_in_buffer = 0
    for byte in raw_bytes:
        buffer |= byte << bits_in_buffer
        bits_in_buffer += 8
        while bits_in_buffer >= 7:
            septets.append(buffer & 0x7F)
            buffer >>= 7
            bits_in_buffer -= 7
    return septets


def decode_possible_hex(text):
    """Try, in order: the simpler ASCII-hex interpretation (2 hex
    digits/byte - correct for most Kenyan telco content, which is plain
    Latin-script text even when a network happens to hex-encode it), then
    UCS2 (4 hex digits/UTF-16 unit) for genuinely Unicode content, then
    (if the text doesn't look like hex at all AND doesn't already look like
    clean text) packed 7-bit GSM septets for firmware that sends those raw
    instead of hex-representing them. The "doesn't already look clean"
    guard matters: without it, already-correct plain text could coincidence-
    ally re-decode into different (wrong) text under the septet unpacking.
    Every check requires the result to look like real ASCII-range text
    rather than just "some Unicode script or other" - Python's
    str.isprintable() accepts CJK/etc, which caused a false-positive decode
    in an earlier version of this heuristic. Returns the original text
    unchanged if nothing produces sensible output."""
    if looks_like_hex(text):
        return try_decode_ascii_hex(text) or try_decode_ucs2_hex(text) or text
    if _mostly_ascii_printable(text):
        return text
    return try_decode_packed_gsm7(text) or text


def _mostly_ascii_printable(decoded):
    if not decoded:
        return False
    # Basic Latin printable + common whitespace + Latin-1 Supplement/Extended
    # (covers accented characters like é, ñ, which are legitimate in real
    # messages) - but NOT the much higher codepoint ranges (CJK etc.) that
    # caused a wrong UCS2 decode to look "printable" in the original bug.
    ok = sum(1 for ch in decoded if (0x20 <= ord(ch) <= 0x7E) or ch in "\r\n\t" or (0xA0 <= ord(ch) <= 0x2FF))
    return ok / len(decoded) > PRINTABLE_RATIO_THRESHOLD
