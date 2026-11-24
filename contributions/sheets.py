"""
sheets.py - a minimal Google Sheets v4 client authenticated as a SERVICE
ACCOUNT, using only `requests` + `PyJWT[crypto]` (no google-* libraries, so
nothing heavy to install on the Pi).

Setup (once): create a service account in Google Cloud, enable the Sheets
API, download its JSON key, create the spreadsheet, and SHARE it with the
service account's client_email as Editor. Paste the JSON key into the
dashboard (Contributions -> Sync).

Clock safety: the Pi has no battery-backed clock, and Google rejects a
signed token whose timestamp is off by more than a few minutes. Before
signing we read Google's own `Date` header and sign with the corrected time,
so a drifted/unsynced Pi clock does not break syncing.
"""
import email.utils
import json
import time

import jwt
import requests

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
TIMEOUT = 30


class SheetsError(Exception):
    pass


def parse_service_account(raw):
    try:
        info = json.loads(raw)
    except (TypeError, ValueError):
        raise SheetsError("The service account key is not valid JSON.")
    for k in ("client_email", "private_key", "token_uri"):
        if not info.get(k):
            raise SheetsError(f"The service account key is missing '{k}'.")
    return info


def _q(tab):
    return "'" + tab.replace("'", "''") + "'"


class SheetsClient:
    def __init__(self, service_account, spreadsheet_id, http=None):
        self.sa = service_account
        self.spreadsheet_id = spreadsheet_id
        self.http = http or requests
        self._token = None
        self._token_exp = 0
        self._offset = 0.0

    # ------------------------------------------------------------- auth
    def _learn_clock_offset(self):
        try:
            r = self.http.head(self.sa["token_uri"], timeout=15)
            date = r.headers.get("Date")
            if date:
                self._offset = email.utils.parsedate_to_datetime(date).timestamp() - time.time()
        except Exception:
            pass   # fall back to the local clock; a real failure surfaces at the token call

    def _fetch_token(self):
        self._learn_clock_offset()
        now = int(time.time() + self._offset)
        assertion = jwt.encode(
            {"iss": self.sa["client_email"], "scope": SCOPE, "aud": self.sa["token_uri"], "iat": now, "exp": now + 3300},
            self.sa["private_key"], algorithm="RS256")
        r = self.http.post(self.sa["token_uri"], data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}, timeout=TIMEOUT)
        if r.status_code != 200:
            raise SheetsError(f"Google rejected the service account login ({r.status_code}): {self._err(r)}")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600)) - 120

    @staticmethod
    def _err(r):
        try:
            e = r.json()
            return e.get("error_description") or (e.get("error") if isinstance(e.get("error"), str) else e["error"].get("message"))
        except Exception:
            return (r.text or "")[:200]

    def _request(self, method, url, **kw):
        for attempt in (1, 2):
            if not self._token or time.time() >= self._token_exp:
                self._fetch_token()
            headers = {"Authorization": f"Bearer {self._token}"}
            r = self.http.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
            if r.status_code == 401 and attempt == 1:
                self._token = None
                continue
            if r.status_code >= 400:
                hint = ""
                if r.status_code == 403:
                    hint = f" Share the spreadsheet with {self.sa['client_email']} as an Editor."
                elif r.status_code == 404:
                    hint = " Check the spreadsheet ID."
                raise SheetsError(f"Google Sheets error {r.status_code}: {self._err(r)}.{hint}")
            return r.json() if r.content else {}
        raise SheetsError("Google Sheets login failed.")

    # -------------------------------------------------------------- api
    def get_title(self):
        data = self._request("GET", f"{API}/{self.spreadsheet_id}", params={"fields": "properties.title,sheets.properties"})
        return data["properties"]["title"]

    def ensure_tabs(self, names):
        data = self._request("GET", f"{API}/{self.spreadsheet_id}", params={"fields": "sheets.properties"})
        have = {s["properties"]["title"] for s in data.get("sheets", [])}
        missing = [n for n in names if n not in have]
        if missing:
            self._request("POST", f"{API}/{self.spreadsheet_id}:batchUpdate",
                          json={"requests": [{"addSheet": {"properties": {"title": n}}} for n in missing]})
        return missing

    def write_table(self, tab, rows, chunk=1500, ncols=26):
        """Overwrites the tab with `rows` (first row = header), then clears
        whatever is left below (rows removed since last time). USER_ENTERED
        so ISO dates become real dates and numbers real numbers; callers
        prefix text that must stay text (phone numbers, codes) with an
        apostrophe."""
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            rng = f"{_q(tab)}!A{i + 1}"
            self._request("PUT", f"{API}/{self.spreadsheet_id}/values/{requests.utils.quote(rng, safe='')}",
                          params={"valueInputOption": "USER_ENTERED"}, json={"range": rng, "majorDimension": "ROWS", "values": part})
        tail = f"{_q(tab)}!A{len(rows) + 1}:{_col(ncols)}"
        self._request("POST", f"{API}/{self.spreadsheet_id}/values/{requests.utils.quote(tail, safe='')}:clear", json={})


def _col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
