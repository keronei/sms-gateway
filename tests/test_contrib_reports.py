import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contributions import reports, store  # noqa: E402
from tests.contrib_test_utils import TZ, ContribTestCase, sms  # noqa: E402

A, B, C = "+254711000001", "+254722000002", "+254733000003"
WED = datetime(2026, 5, 27, 10, 0, tzinfo=TZ).timestamp()   # a Wednesday


class TestPeriods(unittest.TestCase):
    def r(self, period, now=WED, **kw):
        return reports.resolve_period(period, tz=TZ, now=now, **kw)

    def test_today_yesterday_week_month(self):
        self.assertEqual(self.r("today"), ("2026-05-27 00:00:00", "2026-05-28 00:00:00"))
        self.assertEqual(self.r("yesterday"), ("2026-05-26 00:00:00", "2026-05-27 00:00:00"))
        self.assertEqual(self.r("week"), ("2026-05-25 00:00:00", "2026-06-01 00:00:00"))     # Monday start
        self.assertEqual(self.r("month"), ("2026-05-01 00:00:00", "2026-06-01 00:00:00"))

    def test_month_rolls_over_the_year_and_week_across_month_boundary(self):
        dec = datetime(2026, 12, 15, 9, 0, tzinfo=TZ).timestamp()
        self.assertEqual(self.r("month", now=dec), ("2026-12-01 00:00:00", "2027-01-01 00:00:00"))
        sat = datetime(2026, 5, 2, 9, 0, tzinfo=TZ).timestamp()
        self.assertEqual(self.r("week", now=sat)[0], "2026-04-27 00:00:00")

    def test_custom_is_inclusive_of_the_end_day_and_all_is_unbounded(self):
        self.assertEqual(self.r("custom", start="2026-05-01", end="2026-05-31"), ("2026-05-01 00:00:00", "2026-06-01 00:00:00"))
        self.assertEqual(self.r("custom", start="2026-05-10"), ("2026-05-10 00:00:00", None))
        self.assertEqual(self.r("all"), (None, None))

    def test_timezone_decides_which_day_it_is(self):
        # 22:30 UTC on the 26th is already the 27th in Nairobi (UTC+3)
        ts = datetime(2026, 5, 26, 22, 30, tzinfo=__import__("zoneinfo").ZoneInfo("UTC")).timestamp()
        self.assertEqual(self.r("today", now=ts)[0], "2026-05-27 00:00:00")


class TestReports(ContribTestCase):
    def setUp(self):
        super().setUp()
        with store.tx() as c:
            self.acc2 = store.save_account(c, {"account_ref": "777***1111", "label": "Mission fund", "paybill_id": self.paybill_id})
        # every message "arrives" on the Wednesday so earlier-dated payments are plausible
        # A: Karen  | B: South C | C: no region
        for phone, ward in [(A, "KAR"), (B, "SC")]:
            self.ingest(phone, f"ward {ward}", received_at=WED, now=WED)
        for sender, body in [
            (A, sms("K000000001", amount="200.00", date="24/05/2026")),
            (A, sms("K000000002", amount="300.00", date="26/05/2026")),
            (B, sms("S000000001", amount="1,000.00", date="26/05/2026", time="08:00 AM")),
            (B, sms("S000000002", amount="50.00", date="27/05/2026", account="777***1111")),
            (C, sms("U000000001", amount="75.00", date="25/05/2026")),
        ]:
            self.ingest(sender, body, received_at=WED, now=WED)

    def f(self, **kw):
        return kw

    def test_totals_and_distinct_contributors(self):
        with store.tx() as c:
            s = reports.summary(c, {})
        self.assertEqual((s["total_cents"], s["count"], s["contributors"]), (162500, 5, 3))

    def test_period_filter_uses_the_payment_time_in_the_sms(self):
        with store.tx() as c:
            self.assertEqual(reports.summary(c, {"period": "today"}, now=WED)["total_cents"], 5000)
            self.assertEqual(reports.summary(c, {"period": "week"}, now=WED)["count"], 4)       # 25th-27th
            self.assertEqual(reports.summary(c, {"period": "yesterday"}, now=WED)["total_cents"], 130000)

    def test_region_filter_includes_sub_regions_and_unassigned(self):
        with store.tx() as c:
            self.assertEqual(reports.summary(c, {"region_id": str(self.lan)})["count"], 4, "constituency rolls up its wards")
            self.assertEqual(reports.summary(c, {"region_id": str(self.kar)})["total_cents"], 50000)
            self.assertEqual(reports.summary(c, {"region_id": "none"})["total_cents"], 7500)
            self.assertEqual(reports.summary(c, {"region_id": str(self.kib)})["count"], 0)

    def test_account_filter_and_void_exclusion(self):
        cid = self.contribution("S000000001")["id"]     # looked up before opening our own transaction
        with store.tx() as c:
            self.assertEqual(reports.summary(c, {"account_id": str(self.acc2)})["total_cents"], 5000)
            store.set_contribution_status(c, cid, "void")
            self.assertEqual(reports.summary(c, {})["total_cents"], 62500)
            self.assertEqual(reports.summary(c, {"status": "any"})["count"], 5)
            store.set_contribution_status(c, cid, "valid")
            self.assertEqual(reports.summary(c, {})["total_cents"], 162500)

    def test_progress_table_is_per_region_and_account_with_unassigned_row(self):
        with store.tx() as c:
            rows = reports.progress_table(c, {}, now=WED)
        got = {(r["region"], r["account"]): r["collected_cents"] for r in rows}
        self.assertEqual(got, {("Karen", "Building fund"): 50000, ("South C", "Building fund"): 100000,
                               ("South C", "Mission fund"): 5000, ("Unassigned", "Building fund"): 7500})
        self.assertEqual(rows[-1]["region"], "Unassigned", "unassigned sorts last")

    def test_targets_are_optional_and_drive_percent(self):
        with store.tx() as c:
            store.save_target(c, {"region_id": self.kar, "account_id": self.acc, "amount": "2000"})
            rows = {(r["region"], r["account"]): r for r in reports.progress_table(c, {}, now=WED)}
        k = rows[("Karen", "Building fund")]
        self.assertEqual((k["target_cents"], k["percent"], k["progress_cents"]), (200000, 25.0, 50000))
        self.assertIsNone(rows[("South C", "Building fund")]["percent"])

    def test_target_only_region_appears_with_zero_collected(self):
        with store.tx() as c:
            store.save_target(c, {"region_id": self.kib, "account_id": self.acc, "amount": "10000"})
            rows = {r["region"]: r for r in reports.progress_table(c, {}, now=WED)}
        self.assertEqual((rows["Kibra"]["collected_cents"], rows["Kibra"]["percent"]), (0, 0.0))

    def test_target_period_bounds_what_counts_towards_it(self):
        with store.tx() as c:
            store.save_target(c, {"region_id": self.kar, "account_id": self.acc, "amount": "1000",
                                  "starts_on": "2026-05-25", "ends_on": "2026-05-31"})
            p = store.progress(c, self.kar, self.acc, "2026-05-27")
        self.assertEqual(p["total_cents"], 30000, "the 24th payment predates the target period")
        with store.tx() as c:
            self.assertIsNone(store.progress(c, self.kar, self.acc, "2026-06-15")["target_cents"], "expired target is ignored")

    def test_constituency_target_is_sum_of_ward_targets_unless_it_has_its_own(self):
        with store.tx() as c:
            store.save_target(c, {"region_id": self.kar, "account_id": self.acc, "amount": "1000"})
            store.save_target(c, {"region_id": self.sc, "account_id": self.acc, "amount": "3000"})
            p = store.progress(c, self.lan, self.acc, "2026-05-27")
            self.assertEqual((p["target_cents"], p["total_cents"]), (400000, 150000))
            store.save_target(c, {"region_id": self.lan, "account_id": self.acc, "amount": "9000"})
            self.assertEqual(store.progress(c, self.lan, self.acc, "2026-05-27")["target_cents"], 900000)

    def test_list_is_newest_first_filterable_and_paginates(self):
        with store.tx() as c:
            rows, total = reports.list_contributions(c, {}, limit=2)
            self.assertEqual((total, len(rows)), (5, 2))
            self.assertEqual(rows[0]["txn_code"], "S000000002")
            page2, _ = reports.list_contributions(c, {}, limit=2, offset=2)
            self.assertEqual(len({r["id"] for r in rows + page2}), 4)
            self.assertEqual(reports.list_contributions(c, {"sender_id": "1"})[1], 2)

    def test_daily_series(self):
        with store.tx() as c:
            days = {d["day"]: d["total_cents"] for d in reports.daily(c, {})}
        self.assertEqual(days["2026-05-26"], 130000)

    def test_attention_lists_what_needs_a_human(self):
        self.ingest(C, sms("BAD0000001", paybill="999999"), received_at=WED, now=WED)
        self.ingest("+254744000004", sms("N000000001", account="555***9999", date="27/05/2026"), received_at=WED, now=WED)
        with store.tx() as c:
            a = reports.attention(c)
        self.assertEqual({s["phone"] for s in a["unassigned_senders"]}, {C, "+254744000004"})
        self.assertEqual([m["outcome"] for m in a["messages"]], ["wrong_paybill"])
        self.assertEqual([x["account_ref"] for x in a["new_accounts"]], ["555***9999"])


class TestRegionMatching(ContribTestCase):
    def match(self, text):
        with store.tx() as c:
            r, sugg = store.match_region(c, text)
        return (r["code"] if r else None), [s["code"] for s in sugg]

    def test_exact_alias_and_longest_phrase_wins(self):
        with store.tx() as c:
            store.save_region(c, {"name": "South C Extension", "code": "SCX"})
        self.assertEqual(self.match("kar")[0], "KAR")
        self.assertEqual(self.match("Karen Ward")[0], "KAR")
        self.assertEqual(self.match("south c extension")[0], "SCX")
        self.assertEqual(self.match("i live in south c extension area")[0], "SCX")
        self.assertEqual(self.match("south c")[0], "SC")

    def test_inactive_regions_are_not_matchable(self):
        with store.tx() as c:
            store.save_region(c, {"name": "Karen", "code": "KAR", "parent_id": self.lan, "active": False}, self.kar)
        self.assertIsNone(self.match("karen")[0])

    def test_no_match_gives_close_suggestions_only(self):
        code, sugg = self.match("Karenn")
        self.assertIsNone(code)
        self.assertIn("KAR", sugg)
        self.assertEqual(self.match("zzzzqq"), (None, []))

    def test_deleting_or_cycling_regions_is_guarded(self):
        with store.tx() as c:
            with self.assertRaises(ValueError):
                store.delete_region(c, self.lan)                  # has children
            with self.assertRaises(ValueError):
                store.save_region(c, {"name": "Langata", "code": "LAN", "parent_id": self.kar}, self.lan)   # cycle
            with self.assertRaises(ValueError):
                store.save_region(c, {"name": "Dup", "code": "kar"})                                      # code unique, case-insensitive
            store.delete_region(c, self.kib)                      # unused leaf is fine


if __name__ == "__main__":
    unittest.main()
