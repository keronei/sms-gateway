"""
Tests for dispatcher.py's Android-Gateway-vs-modem backend selection:
_backend_module() picking the right module, _refresh_gateway_statuses()/
_refresh_modem_statuses() making the right delivered/failed/leave-alone
call, and one end-to-end test that a campaign recipient actually gets
sent through the modem path and lands with the right tracking data.

Runs against a real temporary sqlite file (tests/db_test_utils.py).
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import gateway  # noqa: E402
import modem_gateway  # noqa: E402
import dispatcher  # noqa: E402
from tests.db_test_utils import TempDbTestCase  # noqa: E402


class TestBackendModuleSelection(TempDbTestCase):
    def test_defaults_to_android_gateway(self):
        self.assertIs(dispatcher._backend_module({}), gateway)
        self.assertIs(dispatcher._backend_module({"sms_backend": "android_gateway"}), gateway)

    def test_modem_selected_explicitly(self):
        self.assertIs(dispatcher._backend_module({"sms_backend": "modem"}), modem_gateway)

    def test_unknown_value_falls_back_to_android_gateway(self):
        self.assertIs(dispatcher._backend_module({"sms_backend": "something_else"}), gateway)


class TestRefreshModemStatuses(TempDbTestCase):
    def _recipient(self, id, phone, sent_at, refs, status="sent"):
        return {
            "id": id, "phone_normalized": phone, "sent_at": sent_at,
            "gateway_message_id": json.dumps(refs), "status": status,
        }

    def test_all_parts_delivered_marks_recipient_delivered(self):
        now = time.time()
        db.record_sms_ref(mr=1, recipient="+254700000001", part_seq=1, part_total=1)
        db.update_sms_ref_status(1, "+254700000001", "delivered")
        # need a real recipients row since _refresh_modem_statuses calls db.update_recipient(id, ...)
        cid = db.create_campaign("c", "hi {name}", ["name"], "phone", {}, "f.csv")
        db.add_recipients(cid, [{
            "row_index": 0, "phone_raw": "0700000001", "phone_normalized": "+254700000001",
            "data_json": "{}", "filled_message": "hi", "char_count": 2, "segment_count": 1,
            "status": "sent", "error": None, "updated_at": now,
        }])
        r = db.get_recipients(cid)[0]
        db.update_recipient(r["id"], gateway_message_id=json.dumps([1]), sent_at=now)
        r = db.get_recipients(cid)[0]

        updated = dispatcher._refresh_modem_statuses([r])
        self.assertEqual(updated, 1)
        self.assertEqual(db.get_recipients(cid)[0]["status"], "delivered")

    def test_any_failed_part_marks_recipient_failed(self):
        now = time.time()
        db.record_sms_ref(mr=2, recipient="+254700000002", part_seq=1, part_total=2)
        db.record_sms_ref(mr=3, recipient="+254700000002", part_seq=2, part_total=2)
        db.update_sms_ref_status(2, "+254700000002", "delivered")
        db.update_sms_ref_status(3, "+254700000002", "failed")
        cid = db.create_campaign("c", "hi", [], "phone", {}, "f.csv")
        db.add_recipients(cid, [{
            "row_index": 0, "phone_raw": "0700000002", "phone_normalized": "+254700000002",
            "data_json": "{}", "filled_message": "hi", "char_count": 2, "segment_count": 1,
            "status": "sent", "error": None, "updated_at": now,
        }])
        r = db.get_recipients(cid)[0]
        db.update_recipient(r["id"], gateway_message_id=json.dumps([2, 3]), sent_at=now)
        r = db.get_recipients(cid)[0]

        updated = dispatcher._refresh_modem_statuses([r])
        self.assertEqual(updated, 1)
        self.assertEqual(db.get_recipients(cid)[0]["status"], "failed")

    def test_still_pending_part_leaves_recipient_alone(self):
        now = time.time()
        db.record_sms_ref(mr=4, recipient="+254700000003", part_seq=1, part_total=2)
        # part 2's +CDS report hasn't arrived - no row for mr=5 at all
        cid = db.create_campaign("c", "hi", [], "phone", {}, "f.csv")
        db.add_recipients(cid, [{
            "row_index": 0, "phone_raw": "0700000003", "phone_normalized": "+254700000003",
            "data_json": "{}", "filled_message": "hi", "char_count": 2, "segment_count": 1,
            "status": "sent", "error": None, "updated_at": now,
        }])
        r = db.get_recipients(cid)[0]
        db.update_recipient(r["id"], gateway_message_id=json.dumps([4, 5]), sent_at=now)
        r = db.get_recipients(cid)[0]

        updated = dispatcher._refresh_modem_statuses([r])
        self.assertEqual(updated, 0)
        self.assertEqual(db.get_recipients(cid)[0]["status"], "sent")

    def test_recipient_from_the_other_backend_is_skipped_not_errored(self):
        """gateway_message_id from an Android-gateway send is a plain
        opaque string id, not a JSON list - must not raise."""
        cid = db.create_campaign("c", "hi", [], "phone", {}, "f.csv")
        db.add_recipients(cid, [{
            "row_index": 0, "phone_raw": "0700000004", "phone_normalized": "+254700000004",
            "data_json": "{}", "filled_message": "hi", "char_count": 2, "segment_count": 1,
            "status": "sent", "error": None, "updated_at": time.time(),
        }])
        r = db.get_recipients(cid)[0]
        db.update_recipient(r["id"], gateway_message_id="opaque-android-id-123", sent_at=time.time())
        r = db.get_recipients(cid)[0]

        updated = dispatcher._refresh_modem_statuses([r])
        self.assertEqual(updated, 0)


class TestEndToEndModemSend(TempDbTestCase):
    def test_worker_sends_a_recipient_via_modem_and_records_refs(self):
        db.save_settings({"sms_backend": "modem", "delay_seconds": 0, "batch_size": 0})
        cid = db.create_campaign("c", "hi {name}", ["name"], "phone", {}, "f.csv")
        db.add_recipients(cid, [{
            "row_index": 0, "phone_raw": "0700000009", "phone_normalized": "+254700000009",
            "data_json": "{}", "filled_message": "hi there", "char_count": 8, "segment_count": 1,
            "status": "pending", "error": None, "updated_at": time.time(),
        }])

        # stand in for the modem daemon completing whatever gets enqueued
        def daemon_stub():
            deadline = time.time() + 2
            while time.time() < deadline:
                pending = db.get_pending_modem_commands()
                if pending:
                    db.complete_modem_command(
                        pending[0]["id"], "done", '{"message_refs": [99], "parts": 1}'
                    )
                    return
                time.sleep(0.01)
        threading.Thread(target=daemon_stub, daemon=True).start()

        import modem_gateway as mg
        real_timeout = mg.POLL_TIMEOUT
        mg.POLL_TIMEOUT = 3
        try:
            t = dispatcher.start_campaign(cid)
            self.assertTrue(t)
            deadline = time.time() + 3
            while dispatcher.is_running(cid) and time.time() < deadline:
                time.sleep(0.02)
        finally:
            mg.POLL_TIMEOUT = real_timeout

        r = db.get_recipients(cid)[0]
        self.assertEqual(r["status"], "sent")
        self.assertEqual(json.loads(r["gateway_message_id"]), [99])


if __name__ == "__main__":
    import unittest
    unittest.main()
