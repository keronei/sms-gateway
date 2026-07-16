"""
merge.py - mail merge logic: template field detection, CSV parsing/validation,
phone number normalization and SMS segment counting.
"""
import re
import csv
import io

FIELD_RE = re.compile(r"\{\s*([a-zA-Z0-9_.\-]+)\s*\}")

# Common header names we try to auto-detect as "the phone number column"
PHONE_HEADER_CANDIDATES = [
    "phone", "phone_number", "phonenumber", "mobile", "mobile_number",
    "msisdn", "cell", "tel", "telephone", "contact", "number",
]

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def detect_fields(template_text: str):
    """Return the ordered, de-duplicated list of {field} placeholders in a template."""
    seen = []
    for m in FIELD_RE.finditer(template_text or ""):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def guess_phone_column(headers):
    lower = {h.lower().strip(): h for h in headers}
    for candidate in PHONE_HEADER_CANDIDATES:
        if candidate in lower:
            return lower[candidate]
    # fallback: header that contains "phone" or "mobile" or "tel"
    for h in headers:
        hl = h.lower()
        if "phone" in hl or "mobile" in hl or "tel" in hl or "msisdn" in hl:
            return h
    return None


def parse_csv(file_bytes: bytes):
    """Parse CSV bytes -> (headers, list_of_row_dicts). Tries utf-8-sig then latin-1."""
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Could not decode CSV file (unsupported encoding).")

    # sniff dialect, fall back to comma
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for raw_row in reader:
        row = {}
        for k, v in raw_row.items():
            if k is None:
                continue
            row[k.strip()] = (v or "").strip()
        rows.append(row)
    return headers, rows


def normalize_phone(raw: str, default_country_code: str = ""):
    """Best-effort E.164-ish normalization. Returns (normalized, is_valid)."""
    if raw is None:
        return "", False
    digits = re.sub(r"[^\d+]", "", raw.strip())
    if not digits:
        return "", False

    if digits.startswith("00"):
        digits = "+" + digits[2:]

    if not digits.startswith("+"):
        if default_country_code:
            cc = default_country_code.lstrip("+")
            # strip a leading 0 (common local-format convention) before prepending country code
            local = digits.lstrip("0")
            digits = f"+{cc}{local}"
        else:
            digits = "+" + digits

    digit_count = len(re.sub(r"\D", "", digits))
    is_valid = 8 <= digit_count <= 15
    return digits, is_valid


def sms_segments(text: str):
    """Return (char_count, is_unicode, segment_count) using standard GSM-7/UCS-2 rules."""
    char_count = len(text)
    is_unicode = any(ch not in GSM7_BASIC for ch in text)
    if is_unicode:
        single_limit, multi_limit = 70, 67
    else:
        single_limit, multi_limit = 160, 153
    if char_count == 0:
        return 0, is_unicode, 0
    if char_count <= single_limit:
        segments = 1
    else:
        segments = -(-char_count // multi_limit)  # ceil division
    return char_count, is_unicode, segments


def fill_template(template_text: str, row: dict, mapping: dict):
    """Replace {field} in template_text using row values, resolved through the
    field -> csv_column mapping. Missing values become empty string but are flagged."""
    missing = []

    def _replace(m):
        field = m.group(1)
        column = mapping.get(field, field)
        value = row.get(column, None)
        if value is None or value == "":
            missing.append(field)
            return ""
        return str(value)

    filled = FIELD_RE.sub(_replace, template_text or "")
    return filled, missing


def build_preview(template_text, headers, rows, phone_column, mapping, default_country_code=""):
    """Build the full list of merged messages + validation info for a CSV + template."""
    fields = detect_fields(template_text)
    results = []
    for idx, row in enumerate(rows):
        filled, missing = fill_template(template_text, row, mapping)
        phone_raw = row.get(phone_column, "") if phone_column else ""
        phone_norm, phone_valid = normalize_phone(phone_raw, default_country_code)
        char_count, is_unicode, segments = sms_segments(filled)

        status = "pending"
        error = None
        if not phone_column or not phone_raw:
            status = "invalid"
            error = "Missing phone number"
        elif not phone_valid:
            status = "invalid"
            error = f"Unrecognized phone format: '{phone_raw}'"
        elif missing:
            status = "invalid"
            error = f"Missing value(s) for: {', '.join(missing)}"

        results.append({
            "row_index": idx,
            "phone_raw": phone_raw,
            "phone_normalized": phone_norm,
            "data": row,
            "filled_message": filled,
            "char_count": char_count,
            "is_unicode": is_unicode,
            "segment_count": segments,
            "status": status,
            "error": error,
        })
    return fields, results


def validate_csv_for_template(headers, template_fields, phone_column):
    """Check whether the CSV headers cover the template fields + a phone column.
    Returns dict: {ok, missing_fields, phone_column_found}"""
    missing_fields = [f for f in template_fields if f not in headers]
    return {
        "ok": not missing_fields and bool(phone_column),
        "missing_fields": missing_fields,
        "phone_column_found": bool(phone_column),
    }
