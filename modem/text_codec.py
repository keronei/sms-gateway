"""
text_codec.py - best-effort decoding of AT response text that might actually
be hex-encoded bytes rather than already-readable text.

Both SMS bodies (AT+CMGL/CMGR in text mode) and USSD replies (+CUSD:) can
come back as a hex string instead of plain text - modems commonly fall back
to this whenever the underlying message used an encoding (typically UCS2)
that can't be represented in the currently-configured character set. There
are two hex-encoding shapes to watch for:

  - UCS2: 4 hex digits per UTF-16BE code unit (e.g. "0048" = 'H')
  - raw 8-bit/ASCII bytes: 2 hex digits per byte (e.g. "48" = 'H')

We can't always know which one applies just from a dcs/status flag (and
different firmware/carriers are inconsistent about it), so this module
detects "does this look like hex" and tries both interpretations, keeping
whichever produces mostly-printable text. Relies on the serial transport
layer (serial_at.py) preserving raw byte values losslessly (decoding as
latin-1, not UTF-8-with-replacement) - otherwise the bytes needed here would
already be destroyed before reaching this code.
"""

PRINTABLE_RATIO_THRESHOLD = 0.8


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


def decode_possible_hex(text):
    """Try the simpler ASCII-hex interpretation first (2 hex digits/byte -
    correct for most Kenyan telco content, which is plain Latin-script text
    even when a network happens to hex-encode it), then UCS2 (4 hex
    digits/UTF-16 unit) as a fallback for genuinely Unicode content. Both
    checks require the result to look like real ASCII-range text rather than
    just "some Unicode script or other" - Python's str.isprintable() accepts
    CJK/etc, which caused false-positive UCS2 decodes of what was actually
    plain ASCII-hex data. Returns the original text unchanged if it doesn't
    look like hex at all, or if neither decode produces sensible output."""
    if not looks_like_hex(text):
        return text
    return try_decode_ascii_hex(text) or try_decode_ucs2_hex(text) or text


def _mostly_ascii_printable(decoded):
    if not decoded:
        return False
    # Basic Latin printable + common whitespace + Latin-1 Supplement/Extended
    # (covers accented characters like é, ñ, which are legitimate in real
    # messages) - but NOT the much higher codepoint ranges (CJK etc.) that
    # caused a wrong UCS2 decode to look "printable" in the original bug.
    ok = sum(1 for ch in decoded if (0x20 <= ord(ch) <= 0x7E) or ch in "\r\n\t" or (0xA0 <= ord(ch) <= 0x2FF))
    return ok / len(decoded) > PRINTABLE_RATIO_THRESHOLD
