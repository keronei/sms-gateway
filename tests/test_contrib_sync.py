import email.utils
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import db  # noqa: E402
from contributions import sheets, store, sync  # noqa: E402
from tests.contrib_test_utils import ContribTestCase, sms  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
SA = {"client_email": "bot@proj.iam.gserviceaccount.com", "private_key": PEM, "token_uri": "https://oauth2.googleapis.com/token"}
A = "+254711000001"


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.content = b"x" if body is not None else b""
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


class FakeGoogle:
    """Just enough of the token endpoint and Sheets v4 to exercise the client."""
    def __init__(self, server_time_offset=0.0, tabs=("Sheet1",)):
        self.offset, self.tabs = server_time_offset, list(tabs)
        self.assertions, self.writes, self.clears, self.calls = [], [], [], []
        self.fail_next = []          # list of (status, body) consumed by sheets calls
        self.token_calls = 0

    def head(self, url, timeout=None):
        return Resp(200, None, {"Date": email.utils.formatdate(time.time() + self.offset, usegmt=True)})

    def post(self, url, data=None, timeout=None):
        self.token_calls += 1
        self.assertions.append(data["assertion"])
        return Resp(200, {"access_token": f"tok{self.token_calls}", "expires_in": 3600})

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):    # noqa: A002
        self.calls.append((method, url))
        if self.fail_next:
            status, body = self.fail_next.pop(0)
            return Resp(status, body)
        if url.endswith(":batchUpdate"):
            self.tabs += [r["addSheet"]["properties"]["title"] for r in json["requests"]]
            return Resp(200, {})
        if url.endswith(":clear"):
            self.clears.append(url)
            return Resp(200, {})
        if method == "PUT":
            self.writes.append(json)
            return Resp(200, {})
        return Resp(200, {"properties": {"title": "My Sheet"}, "sheets": [{"properties": {"title": t}} for t in self.tabs]})


class TestSheetsClient(unittest.TestCase):
    def client(self, g):
        return sheets.SheetsClient(SA, "SHEETID", http=g)

    def test_signs_a_valid_jwt_for_the_service_account(self):
        g = FakeGoogle()
        self.client(g).get_title()
        claims = jwt.decode(g.assertions[0], KEY.public_key(), algorithms=["RS256"], audience=SA["token_uri"])
        self.assertEqual((claims["iss"], claims["scope"]), (SA["client_email"], sheets.SCOPE))

    def test_a_badly_wrong_pi_clock_is_corrected_from_googles_date_header(self):
        # the Pi thinks it is 2 days earlier than Google does: unsigned-token-rejection territory
        g = FakeGoogle(server_time_offset=2 * 86400)
        self.client(g).get_title()
        iat = jwt.decode(g.assertions[0], options={"verify_signature": False})["iat"]
        self.assertAlmostEqual(iat, time.time() + 2 * 86400, delta=30)

    def test_token_is_reused_and_refreshed_once_on_401(self):
        g = FakeGoogle()
        c = self.client(g)
        c.get_title()
        c.get_title()
        self.assertEqual(g.token_calls, 1)
        g.fail_next = [(401, {"error": {"message": "expired"}})]
        c.get_title()
        self.assertEqual(g.token_calls, 2)

    def test_errors_have_actionable_hints(self):
        g = FakeGoogle()
        g.fail_next = [(403, {"error": {"message": "The caller does not have permission"}})]
        with self.assertRaises(sheets.SheetsError) as cm:
            self.client(g).get_title()
        self.assertIn(SA["client_email"], str(cm.exception))
        g.fail_next = [(404, {"error": {"message": "Requested entity was not found."}})]
        with self.assertRaises(sheets.SheetsError) as cm:
            self.client(g).get_title()
        self.assertIn("spreadsheet ID", str(cm.exception))

    def test_ensure_tabs_creates_only_missing_ones(self):
        g = FakeGoogle(tabs=("Sheet1", "Summary"))
        self.assertEqual(self.client(g).ensure_tabs(["Contributions", "Summary"]), ["Contributions"])
        self.assertEqual(g.tabs.count("Summary"), 1)

    def test_write_table_chunks_and_clears_leftover_rows(self):
        g = FakeGoogle()
        rows = [["h"]] + [[i] for i in range(7)]
        self.client(g).write_table("Contributions", rows, chunk=3)
        self.assertEqual([len(w["values"]) for w in g.writes], [3, 3, 2])
        self.assertIn("A1", g.writes[0]["range"])
        self.assertIn("A4", g.writes[1]["range"])
        self.assertEqual(len(g.clears), 1)
        self.assertIn("A9", g.clears[0].replace("%21", "!").replace("%3A", ":"))

    def test_tab_names_with_quotes_are_escaped(self):
        g = FakeGoogle()
        self.client(g).write_table("Tom's tab", [["h"]])
        self.assertIn("Tom''s tab", g.writes[0]["range"])

    def test_service_account_parsing(self):
        self.assertEqual(sheets.parse_service_account(json.dumps(SA))["client_email"], SA["client_email"])
        for bad in ["", "nope", json.dumps({"client_email": "a"})]:
            with self.assertRaises(sheets.SheetsError):
                sheets.parse_service_account(bad)


class FakeClient:
    events = []
    fail_on_write = False
    on_write = None

    def __init__(self, sa, spreadsheet_id, http=None):
        self.id = spreadsheet_id

    def get_title(self):
        FakeClient.events.append("title")
        return "My Sheet"

    def ensure_tabs(self, names):
        FakeClient.events.append("tabs")
        return []

    def write_table(self, tab, rows, **kw):
        FakeClient.events.append(f"write:{tab}")
        if FakeClient.fail_on_write:
            raise sheets.SheetsError("Google Sheets error 500")
        FakeClient.tables = getattr(FakeClient, "tables", {})
        FakeClient.tables[tab] = rows
        if FakeClient.on_write:
            FakeClient.on_write()


class SyncTestCase(ContribTestCase):
    def setUp(self):
        super().setUp()
        FakeClient.events, FakeClient.fail_on_write, FakeClient.on_write = [], False, None
        self.daemon = []            # commands sent to the modem daemon
        self.net = {"online": False}
        self.ppp = {"state": "connected"}
        self.outcome = {"ppp_sync_connect": "started"}
        self.patches = [
            mock.patch.object(sync.sheets, "SheetsClient", FakeClient),
            mock.patch.object(sync, "have_internet", lambda timeout=5: self.net["online"]),
            mock.patch.object(sync, "_sleep", lambda s: time.sleep(0.005)),
            mock.patch.object(sync, "_run_daemon_command", self._daemon),
            mock.patch.object(sync, "CONNECT_TIMEOUT", 0.2),
            mock.patch.object(sync, "NET_TIMEOUT", 0.2),
            mock.patch.object(db, "get_modem_status", lambda: {"ppp_state": self.ppp["state"], "ppp_last_error": "no carrier"}),
        ]
        [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.settings(enabled=1, sheets_enabled=1, sheets_spreadsheet_id="SHEET", sheets_service_account_json=json.dumps(SA))
        self.ingest(A, sms())

    def _daemon(self, name, timeout=15):
        self.daemon.append(name)
        FakeClient.events.append("daemon:" + name)
        if name == "ppp_sync_connect":
            self.net["online"] = self.ppp["state"] == "connected"      # link up -> internet reachable
        return self.outcome.get(name, "")

    def state(self):
        return self.q("SELECT * FROM sheet_sync_state")[0]


class TestSyncWindow(SyncTestCase):
    def test_already_online_pushes_without_touching_the_modem(self):
        self.net["online"] = True
        r = sync.run_sync()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.daemon, [])
        self.assertEqual(FakeClient.events, ["tabs", "write:Contributions", "write:Summary"])
        st = self.state()
        self.assertEqual(st["synced_version"], st["data_version"])
        self.assertIsNone(st["last_error"])

    def test_offline_opens_a_session_pushes_then_closes_it(self):
        r = sync.run_sync()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.daemon, ["ppp_sync_connect", "ppp_disconnect"])
        self.assertEqual(FakeClient.events, ["daemon:ppp_sync_connect", "tabs", "write:Contributions", "write:Summary", "daemon:ppp_disconnect"])

    def test_the_link_is_closed_even_when_the_push_fails_and_data_stays_pending(self):
        FakeClient.fail_on_write = True
        r = sync.run_sync()
        self.assertFalse(r["ok"])
        self.assertIn("500", r["error"])
        self.assertEqual(self.daemon[-1], "ppp_disconnect")
        st = self.state()
        self.assertGreater(st["data_version"], st["synced_version"])
        self.assertIn("500", st["last_error"])

    def test_a_link_the_user_already_requested_is_never_torn_down(self):
        self.outcome["ppp_sync_connect"] = "already_connected"
        r = sync.run_sync()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.daemon, ["ppp_sync_connect"], "no ppp_disconnect: we did not bring it up")

    def test_modem_that_cannot_get_online_reports_why_pushes_nothing_and_still_cleans_up(self):
        self.ppp["state"] = "backoff"
        r = sync.run_sync()
        self.assertFalse(r["ok"])
        self.assertIn("no carrier", r["error"])
        self.assertNotIn("write:Contributions", FakeClient.events)
        self.assertEqual(self.daemon, ["ppp_sync_connect", "ppp_disconnect"])

    def test_connected_but_no_route_to_google(self):
        def link_up_but_no_internet(name, timeout=15):
            self.daemon.append(name)
            return "started"                                   # PPP "connects" but have_internet stays False
        with mock.patch.object(sync, "_run_daemon_command", link_up_but_no_internet):
            r = sync.run_sync()
        self.assertFalse(r["ok"])
        self.assertIn("unreachable", r["error"])
        self.assertEqual(self.daemon, ["ppp_sync_connect", "ppp_disconnect"], "still closes the link it opened")

    def test_modem_use_can_be_disabled(self):
        self.settings(sheets_use_modem=0)
        r = sync.run_sync()
        self.assertFalse(r["ok"])
        self.assertEqual(self.daemon, [])

    def test_not_configured_is_a_clear_error(self):
        self.settings(sheets_spreadsheet_id="")
        self.assertIn("spreadsheet", sync.run_sync()["error"].lower())
        self.settings(sheets_spreadsheet_id="X", sheets_service_account_json="")
        self.assertIn("key", sync.run_sync()["error"].lower())

    def test_changes_made_during_a_push_are_not_lost(self):
        self.net["online"] = True

        def concurrent_edit():
            with store.tx() as c:
                store.bump_version(c)
        FakeClient.on_write = concurrent_edit
        self.assertTrue(sync.run_sync()["ok"])
        st = self.state()
        self.assertGreater(st["data_version"], st["synced_version"], "must stay pending for the next sync")

    def test_only_one_sync_at_a_time(self):
        self.net["online"] = True
        sync._LOCK.acquire()
        try:
            self.assertIn("already running", sync.run_sync()["error"])
        finally:
            sync._LOCK.release()

    def test_connection_test_reads_the_title_over_the_same_window(self):
        r = sync.test_connection()
        self.assertTrue(r["ok"])
        self.assertIn("My Sheet", r["message"])
        self.assertEqual(self.daemon, ["ppp_sync_connect", "ppp_disconnect"])


class TestScheduling(SyncTestCase):
    def go(self, now):
        return sync.maybe_run(now=now)

    def test_runs_when_there_is_something_new_and_not_otherwise(self):
        self.net["online"] = True
        t = time.time()
        self.assertTrue(self.go(t)["ok"])
        self.assertIsNone(self.go(t + 7200), "nothing changed, so it must not even go online")
        self.assertEqual(self.daemon, [])

    def test_respects_the_interval_after_a_success(self):
        self.net["online"] = True
        t = time.time()
        self.go(t)
        self.ingest(A, sms("NEWCODE001"))
        self.assertIsNone(self.go(t + 30 * 60), "hourly: too soon")
        self.assertTrue(self.go(t + 61 * 60)["ok"])

    def test_retries_a_failure_sooner_than_the_normal_interval(self):
        t = time.time()
        FakeClient.fail_on_write = True
        self.assertFalse(self.go(t)["ok"])
        FakeClient.fail_on_write = False
        self.assertIsNone(self.go(t + 5 * 60), "not before the retry delay")
        self.assertTrue(self.go(t + 11 * 60)["ok"], "retry well before the hourly interval")

    def test_does_nothing_when_feature_or_sheet_sync_is_off_or_unconfigured(self):
        t = time.time()
        for kw in [dict(enabled=0), dict(sheets_enabled=0), dict(sheets_spreadsheet_id="")]:
            self.settings(enabled=1, sheets_enabled=1, sheets_spreadsheet_id="SHEET")
            self.settings(**kw)
            self.assertIsNone(self.go(t), kw)
        self.assertEqual(self.daemon, [])

    def test_status_summary(self):
        with db.get_conn() as c:
            st = sync.status(c)
        self.assertEqual((st["configured"], st["pending"], st["enabled"]), (True, True, True))
        self.assertEqual(st["client_email"], SA["client_email"])


class TestSheetContents(SyncTestCase):
    def build(self):
        with db.get_conn() as c:
            return sync.build_tables(c, store.get_settings(c))

    def test_contributions_and_summary_rows(self):
        self.ingest(A, "ward KAR")
        self.ingest("+254722000002", sms("BBBBBBBBBB", amount="1,500.50"))
        contrib, summary = self.build()
        self.assertEqual(contrib[0][:4], ["Txn code", "Paid at", "Amount (Ksh)", "Sender phone"])
        by_code = {r[0].lstrip("'"): r for r in contrib[1:]}
        r = by_code["UEOQP5BEC6"]
        self.assertEqual((r[1], r[2], r[3], r[5]), ("2026-05-24 11:27:00", 200.0, "'" + A, "Karen"))
        self.assertTrue(r[0].startswith("'"), "codes and phones are forced to text so Sheets cannot mangle them")
        self.assertEqual(by_code["BBBBBBBBBB"][2], 1500.5)
        self.assertEqual(by_code["BBBBBBBBBB"][5], "Unassigned")
        rows = {(s[0], s[2]): s for s in summary[1:]}
        self.assertEqual(rows[("Karen", "Building fund")][4], 200.0)
        self.assertEqual(rows[("Unassigned", "Building fund")][4], 1500.5)

    def test_phone_masking_option_and_void_status(self):
        self.settings(sheets_mask_phones=1)
        contrib, _ = self.build()
        self.assertNotIn("711000001", contrib[1][3])
        self.assertTrue(contrib[1][3].startswith("'+2547"))
        cid = self.contribution("UEOQP5BEC6")["id"]
        with store.tx() as c:
            store.set_contribution_status(c, cid, "void")
        contrib, summary = self.build()
        self.assertEqual(contrib[1][9], "void", "voids stay visible in the sheet, flagged")
        self.assertEqual(len(summary), 1, "and no longer count in the summary")


if __name__ == "__main__":
    unittest.main()
