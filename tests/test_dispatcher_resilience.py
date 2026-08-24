"""
Tests for dispatcher.py's resilience features:
  - recipients stuck at 'sending' being retryable / reset on startup
  - the consecutive-failure circuit breaker auto-pausing a campaign
    rather than burning through the whole recipient list
  - resume_interrupted_campaigns() picking up campaigns left mid-dispatch
    and restarting a worker for them

Runs against a real temporary sqlite file (tests/db_test_utils.py) and
monkeypatches gateway.send_message directly (rather than going through
the async modem-command polling machinery tested elsewhere) since these
tests are about dispatcher.py's own control flow, not the backends
themselves.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import gateway  # noqa: E402
import dispatcher  # noqa: E402
from tests.db_test_utils import TempDbTestCase  # noqa: E402


def _recipient(row_index, phone, status="pending"):
    return {
        "row_index": row_index, "phone_raw": phone, "phone_normalized": phone,
        "data_json": "{}", "filled_message": "hi", "char_count": 2, "segment_count": 1,
        "status": status, "error": None, "updated_at": time.time(),
    }


class DispatcherResilienceTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        db.save_settings({"sms_backend": "android_gateway", "delay_seconds": 0, "batch_size": 0})
        self._real_send = gateway.send_message

    def tearDown(self):
        gateway.send_message = self._real_send
        super().tearDown()

    def make_campaign(self, recipients, status="draft"):
        cid = db.create_campaign("c", "hi", [], "phone", {}, "f.csv")
        db.add_recipients(cid, recipients)
        if status != "draft":
            db.set_campaign_status(cid, status)
        return cid


class TestStuckSendingRecovery(DispatcherResilienceTestCase):
    def test_worker_retries_a_recipient_stuck_at_sending(self):
        cid = self.make_campaign([_recipient(0, "+254700000001", status="sending")])
        gateway.send_message = lambda settings, phones, text: {"id": "abc", "state": "Sent"}

        dispatcher._worker(cid)

        r = db.get_recipients(cid)[0]
        self.assertEqual(r["status"], "sent")

    def test_reset_stuck_sending_recipients_bulk_helper(self):
        cid = self.make_campaign([
            _recipient(0, "+254700000001", status="sending"),
            _recipient(1, "+254700000002", status="sent"),
            _recipient(2, "+254700000003", status="pending"),
        ])
        n = db.reset_stuck_sending_recipients()
        self.assertEqual(n, 1)
        statuses = {r["row_index"]: r["status"] for r in db.get_recipients(cid)}
        self.assertEqual(statuses, {0: "pending", 1: "sent", 2: "pending"})

    def test_reset_does_not_touch_other_statuses(self):
        cid = self.make_campaign([
            _recipient(0, "+254700000001", status="failed"),
            _recipient(1, "+254700000002", status="delivered"),
        ])
        n = db.reset_stuck_sending_recipients()
        self.assertEqual(n, 0)
        statuses = {r["row_index"]: r["status"] for r in db.get_recipients(cid)}
        self.assertEqual(statuses, {0: "failed", 1: "delivered"})


class TestConsecutiveFailureCircuitBreaker(DispatcherResilienceTestCase):
    def test_pauses_after_threshold_consecutive_failures(self):
        cid = self.make_campaign([_recipient(i, f"+25470000000{i}") for i in range(10)])
        gateway.send_message = lambda settings, phones, text: (_ for _ in ()).throw(
            gateway.GatewayError("connection refused")
        )

        dispatcher._worker(cid)

        campaign = db.get_campaign(cid)
        self.assertEqual(campaign["status"], "paused")
        self.assertIn("5 consecutive", campaign["last_error"])
        self.assertIn("connection refused", campaign["last_error"])

    def test_leaves_remaining_recipients_pending_not_failed(self):
        cid = self.make_campaign([_recipient(i, f"+25470000000{i}") for i in range(10)])
        gateway.send_message = lambda settings, phones, text: (_ for _ in ()).throw(
            gateway.GatewayError("connection refused")
        )

        dispatcher._worker(cid)

        counts = db.campaign_status_counts(cid)
        self.assertEqual(counts.get("failed"), dispatcher.CONSECUTIVE_FAILURE_PAUSE_THRESHOLD)
        self.assertEqual(counts.get("pending"), 10 - dispatcher.CONSECUTIVE_FAILURE_PAUSE_THRESHOLD)

    def test_a_success_resets_the_consecutive_counter(self):
        """4 failures, then a success, then 4 more failures must NOT
        trigger the breaker - only genuinely consecutive streaks count."""
        cid = self.make_campaign([_recipient(i, f"+25470000000{i}") for i in range(9)])
        calls = {"n": 0}

        def flaky(settings, phones, text):
            calls["n"] += 1
            if calls["n"] == 5:
                return {"id": "ok", "state": "Sent"}
            raise gateway.GatewayError("blip")

        gateway.send_message = flaky
        dispatcher._worker(cid)

        campaign = db.get_campaign(cid)
        # the whole list got processed without the breaker firing (that's
        # the point of this test) - it naturally finishes as "completed"
        # since nothing pending/queued is left, not "paused"
        self.assertEqual(campaign["status"], "completed")
        self.assertIsNone(campaign["last_error"])
        counts = db.campaign_status_counts(cid)
        self.assertEqual(counts.get("sent"), 1)
        self.assertEqual(counts.get("failed"), 8)

    def test_gateway_reported_failure_state_also_counts_toward_the_streak(self):
        """A send that doesn't raise but comes back with state="Failed"
        (a gateway-level rejection, not a transport error) must count
        toward the same consecutive-failure streak."""
        cid = self.make_campaign([_recipient(i, f"+25470000000{i}") for i in range(10)])
        gateway.send_message = lambda settings, phones, text: {"id": "x", "state": "Failed"}

        dispatcher._worker(cid)

        campaign = db.get_campaign(cid)
        self.assertEqual(campaign["status"], "paused")

    def test_few_failures_below_threshold_do_not_pause(self):
        cid = self.make_campaign([_recipient(i, f"+25470000000{i}") for i in range(3)])
        gateway.send_message = lambda settings, phones, text: (_ for _ in ()).throw(
            gateway.GatewayError("bad number")
        )

        dispatcher._worker(cid)

        campaign = db.get_campaign(cid)
        self.assertNotEqual(campaign["status"], "paused")
        counts = db.campaign_status_counts(cid)
        self.assertEqual(counts.get("failed"), 3)

    def test_last_error_cleared_on_a_fresh_start(self):
        cid = self.make_campaign([_recipient(i, f"+25470000000{i}") for i in range(10)])
        gateway.send_message = lambda settings, phones, text: (_ for _ in ()).throw(
            gateway.GatewayError("connection refused")
        )
        dispatcher._worker(cid)
        self.assertIsNotNone(db.get_campaign(cid)["last_error"])

        gateway.send_message = lambda settings, phones, text: {"id": "ok", "state": "Sent"}
        dispatcher.start_campaign(cid)
        deadline = time.time() + 2
        while dispatcher.is_running(cid) and time.time() < deadline:
            time.sleep(0.02)

        self.assertIsNone(db.get_campaign(cid)["last_error"])


class TestResumeInterruptedCampaigns(DispatcherResilienceTestCase):
    def test_dispatching_campaign_is_resumed(self):
        cid = self.make_campaign(
            [_recipient(0, "+254700000001"), _recipient(1, "+254700000002")],
            status="dispatching",
        )
        gateway.send_message = lambda settings, phones, text: {"id": "ok", "state": "Sent"}

        resumed = dispatcher.resume_interrupted_campaigns()
        self.assertEqual(resumed, 1)

        deadline = time.time() + 2
        while dispatcher.is_running(cid) and time.time() < deadline:
            time.sleep(0.02)

        statuses = [r["status"] for r in db.get_recipients(cid)]
        self.assertEqual(statuses, ["sent", "sent"])
        self.assertEqual(db.get_campaign(cid)["status"], "completed")

    def test_paused_campaign_is_not_auto_resumed(self):
        cid = self.make_campaign([_recipient(0, "+254700000001")], status="paused")
        resumed = dispatcher.resume_interrupted_campaigns()
        self.assertEqual(resumed, 0)
        self.assertFalse(dispatcher.is_running(cid))

    def test_stopped_campaign_is_not_auto_resumed(self):
        cid = self.make_campaign([_recipient(0, "+254700000001")], status="stopped")
        resumed = dispatcher.resume_interrupted_campaigns()
        self.assertEqual(resumed, 0)

    def test_completed_campaign_is_not_auto_resumed(self):
        cid = self.make_campaign([_recipient(0, "+254700000001", status="sent")], status="completed")
        resumed = dispatcher.resume_interrupted_campaigns()
        self.assertEqual(resumed, 0)

    def test_stuck_sending_recipient_is_reset_before_resuming(self):
        cid = self.make_campaign(
            [_recipient(0, "+254700000001", status="sending"), _recipient(1, "+254700000002")],
            status="dispatching",
        )
        gateway.send_message = lambda settings, phones, text: {"id": "ok", "state": "Sent"}

        dispatcher.resume_interrupted_campaigns()
        deadline = time.time() + 2
        while dispatcher.is_running(cid) and time.time() < deadline:
            time.sleep(0.02)

        statuses = [r["status"] for r in db.get_recipients(cid)]
        self.assertEqual(statuses, ["sent", "sent"])

    def test_multiple_interrupted_campaigns_all_resume(self):
        cid1 = self.make_campaign([_recipient(0, "+254700000001")], status="dispatching")
        cid2 = self.make_campaign([_recipient(0, "+254700000002")], status="dispatching")
        gateway.send_message = lambda settings, phones, text: {"id": "ok", "state": "Sent"}

        resumed = dispatcher.resume_interrupted_campaigns()
        self.assertEqual(resumed, 2)

        deadline = time.time() + 2
        while (dispatcher.is_running(cid1) or dispatcher.is_running(cid2)) and time.time() < deadline:
            time.sleep(0.02)

        self.assertEqual(db.get_campaign(cid1)["status"], "completed")
        self.assertEqual(db.get_campaign(cid2)["status"], "completed")


if __name__ == "__main__":
    import unittest
    unittest.main()
