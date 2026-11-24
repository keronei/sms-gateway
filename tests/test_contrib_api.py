import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

import db  # noqa: E402
from contributions import api, store  # noqa: E402
from tests.contrib_test_utils import RCV, ContribTestCase, sms  # noqa: E402

A = "+254711000001"


class ApiTestCase(ContribTestCase):
    def setUp(self):
        super().setUp()
        api._tokens.clear()
        api._fails.clear()
        app = Flask(__name__)
        app.register_blueprint(api.bp)
        self.c = app.test_client()

    def post(self, path, body=None, method="post"):
        return getattr(self.c, method)("/api/contrib" + path, json=body or {})


class TestSettingsAndSwitch(ApiTestCase):
    def test_state_reports_switch_and_never_exposes_secrets(self):
        self.post("/settings", {"sheets_service_account_json": '{"client_email":"bot@x.iam","private_key":"k","token_uri":"https://t"}'})
        s = self.c.get("/api/contrib/state").get_json()
        self.assertFalse(s["enabled"])
        self.assertEqual(s["settings"]["sheets_client_email"], "bot@x.iam")
        self.assertTrue(s["settings"]["sheets_service_account_set"])
        self.assertNotIn("sheets_service_account_json", s["settings"])
        self.assertNotIn("pin_hash", s["settings"])
        self.assertNotIn("private_key", self.c.get("/api/contrib/state").get_data(as_text=True))

    def test_enable_and_disable(self):
        self.assertTrue(self.post("/enable", {"enabled": True}).get_json()["enabled"])
        self.assertTrue(self.c.get("/api/contrib/state").get_json()["enabled"])
        self.post("/enable", {"enabled": False})
        self.assertFalse(self.c.get("/api/contrib/state").get_json()["enabled"])

    def test_settings_are_validated(self):
        for bad in [{"parser_pattern": "(unclosed"}, {"parser_pattern": r"(?P<amount>\d)"}, {"timezone": "Mars/Base"},
                    {"reply_max_per_hour": "-3"}, {"sheets_service_account_json": "not json"}]:
            r = self.post("/settings", bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("error", r.get_json())

    def test_settings_cannot_flip_the_switch_or_reach_internals_and_blank_key_is_kept(self):
        self.post("/settings", {"enabled": "1", "inbox_min_id": 99, "pin_hash": "x"})
        s = self.c.get("/api/contrib/state").get_json()
        self.assertFalse(s["enabled"])
        self.post("/settings", {"sheets_service_account_json": '{"client_email":"a@b","private_key":"k","token_uri":"u"}'})
        self.post("/settings", {"sheets_service_account_json": "   ", "sheets_spreadsheet_id": "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=0"})
        s = self.c.get("/api/contrib/state").get_json()["settings"]
        self.assertTrue(s["sheets_service_account_set"], "blank must not erase the stored key")
        self.assertEqual(s["sheets_spreadsheet_id"], "ABC123", "a pasted sheet URL is reduced to its ID")

    def test_parser_test_and_template_preview(self):
        r = self.post("/parser/test", {"body": sms()}).get_json()
        self.assertEqual((r["matches"][0]["code"], r["matches"][0]["amount"]), ("UEOQP5BEC6", "200"))
        self.assertEqual(self.post("/parser/test", {"body": "nothing"}).get_json()["matches"], [])
        p = self.post("/templates/preview", {"template": "Hi {region}: {region_total} of {target}"}).get_json()
        self.assertEqual(p["text"], "Hi Karen: 31,000 of 200,000")
        self.assertEqual(self.post("/templates/preview", {"template": "x" * 200}).get_json()["segments"], 2)


class TestSetupEndpoints(ApiTestCase):
    def test_regions_crud_bulk_and_guards(self):
        self.assertEqual(self.post("/regions", {"name": "Dup", "code": "kar"}).status_code, 400)
        rid = self.post("/regions", {"name": "Embakasi", "code": "EMB", "aliases": ["Embakasi East"]}).get_json()["id"]
        self.assertEqual(self.post(f"/regions/{rid}", {"name": "Embakasi X", "code": "EMB"}, "put").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/contrib/regions/{rid}").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/contrib/regions/{self.lan}").status_code, 400)   # has wards
        r = self.post("/regions/bulk", {"text": "Ruaraka, RUA\nBad line\nKahawa, KAH, RUA\nOrphan, ORP, NOPE\n\n"}).get_json()
        self.assertEqual(r["created"], 2)
        self.assertEqual(len(r["errors"]), 2)
        names = {x["code"]: x for x in self.c.get("/api/contrib/regions").get_json()}
        self.assertEqual(names["KAH"]["parent_id"], names["RUA"]["id"])

    def test_accounts_paybills_targets(self):
        self.assertEqual(self.post("/accounts", {"account_ref": ""}).status_code, 400)
        aid = self.post("/accounts", {"account_ref": "777***1111", "label": "Mission"}).get_json()["id"]
        self.assertEqual(self.post("/accounts", {"account_ref": "777***1111"}).status_code, 400, "duplicate")
        self.assertEqual(self.post("/paybills", {"number": "123456", "name": "Other"}).status_code, 200)
        self.assertEqual(self.post("/paybills", {"number": "123456"}).status_code, 400)
        # targets: bad inputs are clean 400s, never a 500
        for bad in [{"region_id": self.kar, "account_id": aid, "amount": "abc"}, {"region_id": self.kar, "account_id": aid, "amount": "0"},
                    {"region_id": self.kar, "account_id": None, "amount": "10"}, {"region_id": 9999, "account_id": aid, "amount": "10"},
                    {"region_id": self.kar, "account_id": aid, "amount": "10", "starts_on": "2026-02-01", "ends_on": "2026-01-01"}]:
            self.assertEqual(self.post("/targets", bad).status_code, 400, bad)
        tid = self.post("/targets", {"region_id": self.kar, "account_id": aid, "amount": "20,000"}).get_json()["id"]
        self.assertEqual(self.c.get("/api/contrib/targets").get_json()[0]["amount_cents"], 2000000)
        self.assertEqual(self.c.delete(f"/api/contrib/targets/{tid}").status_code, 200)

    def test_cannot_delete_account_with_contributions(self):
        self.ingest(A, sms())
        self.assertEqual(self.c.delete(f"/api/contrib/accounts/{self.acc}").status_code, 400)


class TestSendersAndContributions(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.ingest(A, sms("AAAAAAAAAA"))
        self.ingest("+254722000002", sms("BBBBBBBBBB", amount="50.00"))

    def test_search_any_format_and_assign_region_backfills(self):
        for q in ["0711000001", "711 000", "+254711000001"]:
            self.assertEqual([s["phone"] for s in self.c.get(f"/api/contrib/senders?q={q}".replace("+", "%2B")).get_json()], [A], q)
        sid = self.c.get("/api/contrib/senders?q=711000001").get_json()[0]["id"]
        r = self.post(f"/senders/{sid}/region", {"region_id": self.kar}).get_json()
        self.assertEqual(r["backfilled"], 1)
        self.assertEqual(self.contribution("AAAAAAAAAA")["region_id"], self.kar)
        self.assertEqual(self.post(f"/senders/{sid}/region", {"region_id": 9999}).status_code, 400)

    def test_notify_flag_queues_an_sms_to_the_sender(self):
        sid = self.c.get("/api/contrib/senders?q=711000001").get_json()[0]["id"]
        before = len(self.replies())
        self.post(f"/senders/{sid}/region", {"region_id": self.kar, "notify": False})
        self.assertEqual(len(self.replies()), before)
        self.post(f"/senders/{sid}/region", {"region_id": self.sc, "notify": True})
        self.assertEqual(self.replies()[-1][0], "region_set")
        self.assertIn("South C", self.replies()[-1][1])

    def test_block_and_name(self):
        sid = self.c.get("/api/contrib/senders?q=711000001").get_json()[0]["id"]
        self.post(f"/senders/{sid}", {"name": "Jane", "blocked": True}, "put")
        s = self.c.get("/api/contrib/senders?q=711000001").get_json()[0]
        self.assertEqual((s["name"], s["blocked"]), ("Jane", 1))
        self.assertEqual(self.post("/senders/9999", {"name": "x"}, "put").status_code, 400)

    def test_listing_filters_pagination_and_void(self):
        d = self.c.get("/api/contrib/contributions?period=all").get_json()
        self.assertEqual(d["total"], 2)
        self.assertEqual(self.c.get("/api/contrib/contributions?limit=1&offset=1").get_json()["rows"][0]["txn_code"] in ("AAAAAAAAAA", "BBBBBBBBBB"), True)
        cid = self.contribution("BBBBBBBBBB")["id"]
        self.assertEqual(self.post(f"/contributions/{cid}/status", {"status": "void"}).status_code, 200)
        self.assertEqual(self.c.get("/api/contrib/summary?period=all").get_json()["summary"]["total_cents"], 20000)
        self.assertEqual(self.post(f"/contributions/{cid}/status", {"status": "bogus"}).status_code, 400)

    def test_csv_export_respects_filters(self):
        r = self.c.get("/api/contrib/contributions/export.csv?region_id=none")
        self.assertEqual(r.mimetype, "text/csv")
        lines = r.get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines[0].split(",")[0], "txn_code")
        self.assertEqual(len(lines), 3)
        self.assertIn("Unassigned", lines[1])

    def test_attention_dismiss_and_account_review(self):
        self.ingest(A, sms("CCCCCCCCCC", paybill="999999"))
        a = self.c.get("/api/contrib/attention").get_json()
        self.assertEqual(len(a["messages"]), 1)
        self.post(f"/messages/{a['messages'][0]['id']}/dismiss")
        self.assertEqual(self.c.get("/api/contrib/attention").get_json()["messages"], [])


class TestPin(ApiTestCase):
    def test_no_pin_means_open(self):
        self.assertEqual(self.c.get("/api/contrib/state").status_code, 200)
        self.assertFalse(self.c.get("/api/contrib/auth/status").get_json()["pin_required"])

    def test_pin_protects_everything_but_the_login(self):
        self.assertTrue(self.post("/auth/pin", {"pin": "4321"}).get_json()["pin_set"])
        for path in ["/state", "/summary", "/senders", "/regions", "/sync/status", "/contributions/export.csv"]:
            r = self.c.get("/api/contrib" + path)
            self.assertEqual(r.status_code, 401, path)
            self.assertTrue(r.get_json()["pin_required"])
        self.assertEqual(self.post("/enable", {"enabled": True}).status_code, 401)
        self.assertEqual(self.post("/auth/pin", {"pin": ""}).status_code, 401, "cannot clear the PIN without logging in")
        st = self.c.get("/api/contrib/auth/status").get_json()
        self.assertEqual((st["pin_required"], st["authenticated"]), (True, False))

    def test_login_wrong_then_right_then_logout(self):
        self.post("/auth/pin", {"pin": "4321"})
        self.assertEqual(self.post("/auth/login", {"pin": "0000"}).status_code, 401)
        self.assertEqual(self.post("/auth/login", {"pin": "4321"}).status_code, 200)
        self.assertEqual(self.c.get("/api/contrib/state").status_code, 200)
        self.post("/auth/logout")
        self.assertEqual(self.c.get("/api/contrib/state").status_code, 401)

    def test_pin_is_stored_hashed_never_plain(self):
        self.post("/auth/pin", {"pin": "4321"})
        with db.get_conn() as conn:
            stored = store.get_settings(conn)["pin_hash"]
        self.assertNotIn("4321", stored)
        self.assertTrue(api._check_pin("4321", stored))
        self.assertFalse(api._check_pin("4322", stored))

    def test_brute_force_lockout(self):
        self.post("/auth/pin", {"pin": "4321"})
        codes = [self.post("/auth/login", {"pin": str(i) * 4}).status_code for i in range(5)]
        self.assertEqual(codes, [401] * 5)
        self.assertEqual(self.post("/auth/login", {"pin": "4321"}).status_code, 429, "even the right PIN is refused while locked")

    def test_changing_and_clearing_when_logged_in(self):
        self.post("/auth/pin", {"pin": "4321"})
        self.post("/auth/login", {"pin": "4321"})
        self.assertEqual(self.post("/auth/pin", {"pin": "12"}).status_code, 400, "too short")
        self.assertEqual(self.post("/auth/pin", {"pin": ""}).status_code, 200)
        self.assertEqual(self.c.get("/api/contrib/state").status_code, 200)
        self.assertFalse(self.c.get("/api/contrib/auth/status").get_json()["pin_required"])


if __name__ == "__main__":
    unittest.main()
