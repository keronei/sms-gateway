import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contributions import service, store  # noqa: E402
from tests.contrib_test_utils import ACCOUNT, NOW, RCV, ContribTestCase, sms  # noqa: E402

A = "+254711000001"
B = "+254722000002"


class TestFirstPayment(ContribTestCase):
    def test_new_sender_is_recorded_unassigned_and_asked_for_a_ward(self):
        r = self.ingest(A, sms())
        self.assertEqual(r["outcome"], "recorded")
        c = self.contribution("UEOQP5BEC6")
        self.assertEqual((c["amount_cents"], c["paid_at"], c["status"]), (20000, "2026-05-24 11:27:00", "valid"))
        self.assertIsNone(c["region_id"])
        (kind, body, phone), = self.replies()
        self.assertEqual((kind, phone), ("ack_unassigned", A))
        self.assertIn("Ksh 200", body)
        self.assertIn("KAR", body)            # example ward code is a real one

    def test_known_account_is_matched_not_recreated(self):
        self.ingest(A, sms())
        self.assertEqual(len(self.q("SELECT * FROM accounts")), 1)
        self.assertEqual(self.contribution("UEOQP5BEC6")["account_id"], self.acc)

    def test_unknown_account_is_created_and_flagged_not_lost(self):
        r = self.ingest(A, sms(account="999***0000"))
        self.assertEqual(r["outcome"], "recorded")
        (acc,) = self.q("SELECT * FROM accounts WHERE account_ref = '999***0000'")
        self.assertEqual(acc["auto_created"], 1)


class TestRegionFlow(ContribTestCase):
    def test_reply_by_code_assigns_and_backfills_earlier_payments(self):
        self.ingest(A, sms("AAAAAAAAAA"))
        self.ingest(A, sms("BBBBBBBBBB", amount="50.00"))
        r = self.ingest(A, "KAR")
        self.assertEqual(r["outcome"], "region_set")
        self.assertEqual({c["region_id"] for c in self.q("SELECT region_id FROM contributions")}, {self.kar})
        self.assertEqual(self.q("SELECT region_id FROM senders")[0]["region_id"], self.kar)
        self.assertIn("region_set", self.reply_kinds())

    def test_later_payments_get_progress_reply_with_region_total(self):
        self.ingest(A, "ward KAR")
        self.ingest(A, sms("AAAAAAAAAA", amount="200.00"))
        self.ingest(B, "ward KAR")
        self.ingest(B, sms("BBBBBBBBBB", amount="31,000.00"))
        body = [b for k, b, ph in self.replies() if k == "ack" and ph == B][0]
        self.assertIn("Ksh 31,200", body)     # both senders' payments are counted for the same ward

    def test_target_template_used_when_a_target_exists(self):
        with store.tx() as c:
            store.save_target(c, {"region_id": self.kar, "account_id": self.acc, "amount": "20000"})
        self.ingest(A, "ward KAR")
        self.ingest(A, sms(amount="5,000.00"))
        body = [b for k, b, _ in self.replies() if k == "ack"][0]
        self.assertIn("of Ksh 20,000", body)
        self.assertIn("25%", body)

    def test_matching_by_name_alias_and_inside_a_sentence(self):
        for i, (text, region) in enumerate([("Karen", self.kar), ("karen ward", self.kar), ("south c", self.sc),
                                            ("I stay in South C ward", self.sc)]):
            phone = f"+25470000{i:04d}"
            self.ingest(phone, sms(f"CODE{i:06d}"))          # creates the sender with a prompt
            self.assertEqual(self.ingest(phone, text)["outcome"], "region_set", text)
            self.assertEqual(self.q("SELECT region_id FROM senders WHERE phone = ?", phone)[0]["region_id"], region, text)

    def test_unknown_ward_gets_suggestions_never_a_guess(self):
        self.ingest(A, sms())
        r = self.ingest(A, "ward Karenn")
        self.assertEqual(r["outcome"], "region_unknown")
        self.assertIsNone(self.q("SELECT region_id FROM senders")[0]["region_id"])
        self.assertIn("Karen (KAR)", self.replies()[-1][1])

    def test_registered_sender_must_use_keyword_to_change_ward_and_history_is_kept(self):
        self.ingest(A, "ward KAR")
        self.ingest(A, sms("AAAAAAAAAA"))
        self.ingest(A, "SC")                                  # no keyword: must NOT move them
        self.assertEqual(self.q("SELECT region_id FROM senders")[0]["region_id"], self.kar)
        self.ingest(A, "ward SC")
        self.assertEqual(self.q("SELECT region_id FROM senders")[0]["region_id"], self.sc)
        self.assertEqual(self.contribution("AAAAAAAAAA")["region_id"], self.kar, "history must not be rewritten")
        self.ingest(A, sms("BBBBBBBBBB"))
        self.assertEqual(self.contribution("BBBBBBBBBB")["region_id"], self.sc)

    def test_stranger_saying_hello_is_not_treated_as_a_ward_reply(self):
        r = self.ingest(A, "hello")
        self.assertEqual(r["outcome"], "unparseable")
        self.assertEqual(self.reply_kinds(), ["help"])

    def test_ward_prompts_stop_after_the_configured_maximum(self):
        self.settings(region_prompt_max=2)
        for i in range(4):
            self.ingest(A, sms(f"CODE{i:06d}00"))
        self.assertEqual(self.reply_kinds(), ["ack_unassigned", "ack_unassigned", "ack_unassigned_quiet", "ack_unassigned_quiet"])


class TestDuplicates(ContribTestCase):
    def test_same_code_twice_records_once_and_notifies(self):
        self.ingest(A, sms())
        r = self.ingest(A, sms())
        self.assertEqual(r["outcome"], "duplicate")
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 1)
        self.assertEqual(self.reply_kinds(), ["ack_unassigned", "duplicate"])
        self.assertEqual(len(self.q("SELECT * FROM duplicate_attempts")), 1)

    def test_another_person_forwarding_the_same_receipt_cannot_steal_or_double_count_it(self):
        self.ingest(A, "ward KAR")
        self.ingest(A, sms())
        self.ingest(B, "ward SC")
        r = self.ingest(B, sms())
        self.assertEqual(r["outcome"], "duplicate")
        c = self.contribution("UEOQP5BEC6")
        self.assertEqual(c["region_id"], self.kar)
        self.assertEqual(self.q("SELECT phone FROM senders WHERE id = ?", c["sender_id"])[0]["phone"], A)
        dup_reply = [b for k, b, ph in self.replies() if k == "duplicate"][0]
        self.assertNotIn(A, dup_reply)        # never reveals who recorded it

    def test_mixed_message_records_new_and_flags_duplicate(self):
        self.ingest(A, sms("AAAAAAAAAA"))
        r = self.ingest(A, sms("AAAAAAAAAA") + " " + sms("BBBBBBBBBB"))
        self.assertEqual(r["outcome"], "recorded")
        self.assertEqual((r["duplicates"], len(r["recorded"])), (["AAAAAAAAAA"], 1))
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 2)

    def test_simultaneous_forwards_of_one_receipt_record_exactly_once(self):
        results, errors = [], []

        def go(i):
            try:
                results.append(self.ingest(f"+25473000{i:04d}", sms("RACERACE12"), source_ref=f"race{i}")["outcome"])
            except Exception as e:             # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["duplicate"] * 7 + ["recorded"])
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 1)


class TestSenderFiltering(ContribTestCase):
    def test_is_real_phone(self):
        for ok in ["+254712345678", "0712345678", "254712345678", "712345678", "00254712345678"]:
            self.assertTrue(service.is_real_phone(ok), ok)
        for bad in ["16120", "22222", "456", "MPESA", "KCB-BANK", "", None, "+1234", "M-PESA"]:
            self.assertFalse(service.is_real_phone(bad), bad)

    def test_short_codes_ignored_even_with_a_default_country_code(self):
        with store.tx() as c:
            c.execute("UPDATE settings SET default_country_code = '254' WHERE id = 1")
        r = self.ingest("16120", sms())
        self.assertEqual(r["outcome"], "ignored")
        self.assertEqual((self.q("SELECT * FROM senders"), self.replies(), self.q("SELECT * FROM contributions")), ([], [], []))

    def test_local_format_numbers_are_normalized_with_a_country_code(self):
        with store.tx() as c:
            c.execute("UPDATE settings SET default_country_code = '254' WHERE id = 1")
        self.ingest("0711000001", sms("AAAAAAAAAA"))
        self.ingest("+254711000001", sms("BBBBBBBBBB"))
        self.assertEqual(len(self.q("SELECT * FROM senders")), 1, "same person in two formats must be one sender")

    def test_connect_keyword_is_left_to_the_modem_daemon(self):
        for body in ["connect", "Connect.", " CONNECT "]:
            self.assertEqual(self.ingest(A, body)["outcome"], "ignored")
        self.assertEqual(self.replies(), [])

    def test_blocked_sender_is_logged_but_never_counted_or_answered(self):
        self.ingest(A, "hello")
        with store.tx() as c:
            store.update_sender(c, 1, blocked=True)
        before = len(self.replies())
        r = self.ingest(A, sms())
        self.assertEqual(r["outcome"], "blocked")
        self.assertEqual((len(self.replies()), self.q("SELECT * FROM contributions")), (before, []))


class TestRejections(ContribTestCase):
    def test_other_paybill_is_rejected_and_flagged(self):
        r = self.ingest(A, sms(paybill="999999"))
        self.assertEqual(r["outcome"], "wrong_paybill")
        self.assertEqual(self.q("SELECT * FROM contributions"), [])
        self.assertEqual(self.q("SELECT review FROM ingest_log")[0]["review"], "open")

    def test_disabled_account_is_rejected(self):
        with store.tx() as c:
            store.save_account(c, {"account_ref": ACCOUNT, "paybill_id": self.paybill_id, "label": "x", "active": False}, self.acc)
        self.assertEqual(self.ingest(A, sms())["outcome"], "rejected")

    def test_future_and_ancient_dates_are_rejected_relative_to_when_the_sms_arrived(self):
        self.assertEqual(self.ingest(A, sms("FUTUREFUTU", date="30/05/2026"))["outcome"], "rejected")
        self.assertEqual(self.ingest(A, sms("OLDOLDOLD1", date="01/01/2025"))["outcome"], "rejected")
        self.assertEqual(self.ingest(A, sms("OKOKOKOKOK", date="20/05/2026"))["outcome"], "recorded")
        self.assertEqual(self.ingest(A, sms("SAMEDAYSAM", time="11:59 PM", date="24/05/2026"))["outcome"], "recorded",
                         "within the grace window of the SMS arrival")

    def test_pi_clock_being_wrong_does_not_reject_valid_payments(self):
        # NOW is deliberately years off; judging uses the SMS arrival time
        self.assertEqual(self.ingest(A, sms(), now=NOW + 5 * 365 * 86400)["outcome"], "recorded")

    def test_broken_pattern_is_an_error_to_review_not_a_crash(self):
        self.settings(parser_pattern="(?P<amount>")
        r = self.ingest(A, sms())
        self.assertEqual(r["outcome"], "error")
        self.assertTrue(r["review"])

    def test_unreadable_payment_lookalike_is_flagged_for_review(self):
        r = self.ingest(A, "Confirmed. You have received Ksh 200 from KCB via M-PESA")
        self.assertEqual((r["outcome"], r["review"]), ("unparseable", True))
        self.assertFalse(self.ingest(B, "see you tomorrow")["review"])


class TestIdempotencyAndReplyControls(ContribTestCase):
    def test_same_source_ref_is_processed_once(self):
        self.assertIsNotNone(self.ingest(A, sms(), source_ref="x1"))
        self.assertIsNone(self.ingest(A, sms("ZZZZZZZZZZ"), source_ref="x1"))
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 1)

    def test_replies_can_be_turned_off_entirely(self):
        self.settings(replies_enabled=0)
        self.ingest(A, sms())
        self.assertEqual(self.replies(), [])
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 1)

    def test_blank_template_sends_nothing_for_that_case_only(self):
        self.settings(tpl_duplicate="")
        self.ingest(A, sms())
        self.ingest(A, sms())
        self.assertEqual(self.reply_kinds(), ["ack_unassigned"])

    def test_per_sender_hourly_cap(self):
        self.settings(reply_max_per_hour=3)
        for i in range(6):
            self.ingest(A, sms(f"CODE{i:06d}XX"))
        self.assertEqual(len(self.replies()), 3)
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 6, "the cap limits replies, never recording")

    def test_help_reply_is_rate_limited_by_cooldown(self):
        self.ingest(A, "what is this")
        self.ingest(A, "hello??")
        self.assertEqual(self.reply_kinds(), ["help"])
        self.ingest(A, "again", now=NOW + 7 * 3600)
        self.assertEqual(self.reply_kinds(), ["help", "help"])

    def test_multiple_accounts_in_one_forward_get_one_ack_each(self):
        self.ingest(A, "ward KAR")
        self.ingest(A, sms("AAAAAAAAAA") + sms("BBBBBBBBBB", account="777***1111"))
        self.assertEqual(self.reply_kinds().count("ack"), 2)


if __name__ == "__main__":
    unittest.main()
