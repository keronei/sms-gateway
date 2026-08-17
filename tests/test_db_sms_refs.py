"""
Tests for db.get_sms_ref_statuses() - the lookup dispatcher.py's modem-
backend delivery-report refresh uses. Runs against a real temporary
sqlite file (see tests/db_test_utils.py) since this is exercising actual
SQL, not just Python logic.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from tests.db_test_utils import TempDbTestCase  # noqa: E402


class TestGetSmsRefStatuses(TempDbTestCase):
    def test_single_part_message_status(self):
        db.record_sms_ref(mr=10, recipient="+254700000001", part_seq=1, part_total=1)
        statuses = db.get_sms_ref_statuses([10], "+254700000001")
        self.assertEqual(statuses, ["sent"])

    def test_reflects_status_updates(self):
        db.record_sms_ref(mr=11, recipient="+254700000001")
        db.update_sms_ref_status(11, "+254700000001", "delivered")
        statuses = db.get_sms_ref_statuses([11], "+254700000001")
        self.assertEqual(statuses, ["delivered"])

    def test_multi_part_returns_all_statuses(self):
        db.record_sms_ref(mr=1, recipient="+254700000002", part_seq=1, part_total=2)
        db.record_sms_ref(mr=2, recipient="+254700000002", part_seq=2, part_total=2)
        db.update_sms_ref_status(1, "+254700000002", "delivered")
        db.update_sms_ref_status(2, "+254700000002", "delivered")
        statuses = db.get_sms_ref_statuses([1, 2], "+254700000002")
        self.assertEqual(sorted(statuses), ["delivered", "delivered"])

    def test_missing_ref_is_simply_absent_not_an_error(self):
        db.record_sms_ref(mr=5, recipient="+254700000003")
        statuses = db.get_sms_ref_statuses([5, 999], "+254700000003")
        self.assertEqual(statuses, ["sent"])  # only the real one comes back

    def test_wrong_recipient_does_not_match(self):
        db.record_sms_ref(mr=7, recipient="+254700000004")
        statuses = db.get_sms_ref_statuses([7], "+254700000099")
        self.assertEqual(statuses, [])

    def test_empty_mrs_returns_empty_without_querying(self):
        self.assertEqual(db.get_sms_ref_statuses([], "+254700000001"), [])

    def test_since_filters_out_a_reused_mr_from_an_earlier_send(self):
        """mr wraps 0-255 and can be reused across sends to the same
        number - `since` must stop a stale earlier ref from being picked
        up as if it belonged to a later send."""
        old_time = time.time() - 3600
        with db.get_conn() as conn:
            conn.execute(
                """INSERT INTO modem_sms_refs (mr, recipient, status, part_seq, part_total, sent_at, updated_at)
                   VALUES (?, ?, 'delivered', 1, 1, ?, ?)""",
                (42, "+254700000005", old_time, old_time),
            )
        # a fresh send reusing the same mr, "now"
        db.record_sms_ref(mr=42, recipient="+254700000005")

        recent_only = db.get_sms_ref_statuses([42], "+254700000005", since=time.time() - 5)
        self.assertEqual(recent_only, ["sent"], "must pick the recent row, not the old delivered one")

    def test_without_since_the_most_recent_row_wins_on_reused_mr(self):
        old_time = time.time() - 3600
        with db.get_conn() as conn:
            conn.execute(
                """INSERT INTO modem_sms_refs (mr, recipient, status, part_seq, part_total, sent_at, updated_at)
                   VALUES (?, ?, 'failed', 1, 1, ?, ?)""",
                (43, "+254700000006", old_time, old_time),
            )
        db.record_sms_ref(mr=43, recipient="+254700000006")  # status defaults to 'sent'

        statuses = db.get_sms_ref_statuses([43], "+254700000006")
        self.assertEqual(statuses, ["sent"])


if __name__ == "__main__":
    import unittest
    unittest.main()
