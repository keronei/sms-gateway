import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contributions import parser  # noqa: E402
from tests.contrib_test_utils import sms  # noqa: E402


class TestParser(unittest.TestCase):
    def test_the_real_sample_message(self):
        sample = ("Ksh 200.00 sent to KCB Pay Bill 522522 for account 133***6248 MINISTRY OF REPENTANCE AND HOLINESS has been "
                  "received on 24/05/2026 at 11:27 AM. M-PESA ref UEOQP5BEC6. To reverse this transaction, SMS this message to 16120.")
        (p,) = parser.parse_payments(sample)
        self.assertEqual((p.code, p.amount_cents, p.account_ref, p.paybill), ("UEOQP5BEC6", 20000, "133***6248", "522522"))
        self.assertEqual(p.paid_at, "2026-05-24 11:27:00")
        self.assertEqual(p.account_name, "MINISTRY OF REPENTANCE AND HOLINESS")
        self.assertIsNone(p.error)

    def test_forward_prefix_and_line_wraps_still_match(self):
        body = "Fwd: " + sms().replace(" for account", "\nfor account").replace("has been", "\r\nhas been")
        self.assertEqual(len(parser.parse_payments(body)), 1)

    def test_several_payments_in_one_forward_and_in_message_dedupe(self):
        body = sms("AAAAAAAAAA") + "\n\n" + sms("BBBBBBBBBB", amount="1,500.50", time="11:05 PM") + sms("AAAAAAAAAA")
        got = parser.parse_payments(body)
        self.assertEqual([p.code for p in got], ["AAAAAAAAAA", "BBBBBBBBBB"])
        self.assertEqual(got[1].amount_cents, 150050)
        self.assertEqual(got[1].paid_at, "2026-05-24 23:05:00")

    def test_non_payments_return_nothing(self):
        for text in ["", None, "hello", "ward karen", "Ksh 200 sent to someone"]:
            self.assertEqual(parser.parse_payments(text), [], text)

    def test_time_edge_cases(self):
        cases = {"12:00 AM": "00:00:00", "12:30 PM": "12:30:00", "11:27 AM": "11:27:00", "11:27AM": "11:27:00", "23:15": "23:15:00"}
        for t, expected in cases.items():
            (p,) = parser.parse_payments(sms(time=t))
            self.assertEqual(p.paid_at, "2026-05-24 " + expected, t)

    def test_two_digit_year(self):
        (p,) = parser.parse_payments(sms(date="03/06/26"))
        self.assertEqual(p.paid_at[:10], "2026-06-03")

    def test_matched_but_unusable_values_carry_an_error_instead_of_vanishing(self):
        (p,) = parser.parse_payments(sms(date="31/02/2026"))
        self.assertIn("date", p.error)
        (p,) = parser.parse_payments(sms(amount="0.00"))
        self.assertIn("amount", p.error)

    def test_pattern_validation(self):
        with self.assertRaises(parser.PatternError):
            parser.compile_pattern("(unclosed")
        with self.assertRaises(parser.PatternError) as cm:
            parser.compile_pattern(r"(?P<amount>\d+)")
        self.assertIn("code", str(cm.exception))
        parser.compile_pattern(parser.DEFAULT_PATTERN)

    def test_custom_pattern_for_a_different_wording(self):
        pat = (r"(?P<code>[A-Z0-9]{10}) Confirmed\. (?P<amount>[\d,.]+) paid to account (?P<account>\S+) on "
               r"(?P<date>\d+/\d+/\d+) at (?P<time>[\d:]+ ?[AP]M)")
        (p,) = parser.parse_payments("QWERTY1234 Confirmed. 300.00 paid to account ABC123 on 2/6/26 at 9:05 AM", pat)
        self.assertEqual((p.code, p.amount_cents, p.account_ref, p.paid_at), ("QWERTY1234", 30000, "ABC123", "2026-06-02 09:05:00"))

    def test_oversized_input_is_bounded(self):
        body = ("x" * 50000) + sms()   # payment beyond the cap is ignored, and it returns quickly
        self.assertEqual(parser.parse_payments(body), [])


if __name__ == "__main__":
    unittest.main()
