import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from contributions import replies, service, store, worker  # noqa: E402
from tests.contrib_test_utils import RCV, ContribTestCase, sms  # noqa: E402

A = "+254711000001"


class WorkerTestCase(ContribTestCase):
    def inbox(self, sender, body, ts=RCV):
        return db.add_modem_inbox_message(sender, body, None, ts)

    def modem_ready(self, ready=True):
        db.update_modem_status(device_present=1 if ready else 0, at_ready=1 if ready else 0, sim_status="ready" if ready else "unknown")


class TestEnableAndIngest(WorkerTestCase):
    def test_disabled_feature_reads_nothing(self):
        self.inbox(A, sms())
        self.assertEqual(worker.ingest_once(), 0)
        self.assertEqual(self.q("SELECT * FROM ingest_log"), [])

    def test_enabling_only_considers_messages_that_arrive_afterwards(self):
        self.inbox(A, sms("OLDOLDOLD1"))
        r = worker.set_enabled(True)
        self.assertEqual((r["enabled"], r["skipped_messages"]), (True, 1))
        self.inbox(A, sms("NEWNEWNEW1"))
        self.assertEqual(worker.ingest_once(), 1)
        self.assertIsNone(self.contribution("OLDOLDOLD1"))
        self.assertIsNotNone(self.contribution("NEWNEWNEW1"))

    def test_backlog_can_be_pulled_in_deliberately(self):
        self.inbox(A, sms("OLDOLDOLD1"))
        worker.set_enabled(True)
        self.assertEqual(worker.process_backlog(0), 1)
        worker.ingest_once()
        self.assertIsNotNone(self.contribution("OLDOLDOLD1"))

    def test_second_pass_is_a_no_op_and_missed_messages_are_picked_up_later(self):
        worker.set_enabled(True)
        self.inbox(A, sms("AAAAAAAAAA"))
        self.assertEqual((worker.ingest_once(), worker.ingest_once()), (1, 0))
        # the daemon delivers a late one (e.g. found by its SIM sweep) - the same loop catches it
        self.inbox(A, sms("BBBBBBBBBB"))
        self.assertEqual(worker.ingest_once(), 1)
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 2)

    def test_toggling_off_and_on_skips_what_arrived_while_off_and_says_so(self):
        worker.set_enabled(True)
        worker.set_enabled(False)
        self.inbox(A, sms("WHILEOFF01"))
        self.assertEqual(worker.ingest_once(), 0)
        r = worker.set_enabled(True)
        self.assertEqual(r["skipped_messages"], 1)

    def test_one_poisoned_message_cannot_wedge_the_queue(self):
        worker.set_enabled(True)
        bad = self.inbox(A, sms("BADBADBAD1"))
        self.inbox(A, sms("GOODGOOD01"))
        real = service.process_message

        def flaky(conn, source, ref, *a, **kw):
            if str(ref) == str(bad):
                raise RuntimeError("boom")
            return real(conn, source, ref, *a, **kw)
        with mock.patch.object(service, "process_message", flaky):
            self.assertEqual(worker.ingest_once(), 2)
        self.assertIsNotNone(self.contribution("GOODGOOD01"))
        (err,) = self.q("SELECT * FROM ingest_log WHERE outcome = 'error'")
        self.assertIn("boom", err["detail"])
        self.assertEqual(worker.ingest_once(), 0, "the failed one is not retried in a hot loop")

    def test_reprocess_rereads_failed_messages_after_a_config_fix_without_double_counting(self):
        worker.set_enabled(True)
        self.inbox(A, sms("OTHERPB001", paybill="111111"))
        self.inbox(A, sms("FINEFINE01"))
        worker.ingest_once()
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 1)
        with store.tx() as c:
            store.save_paybill(c, {"number": "111111", "name": "Second"})
        self.assertEqual(worker.reprocess_failed(), 1, "only the failed message is re-queued")
        worker.ingest_once()
        self.assertEqual(len(self.q("SELECT * FROM contributions")), 2)


class TestReplyOutbox(WorkerTestCase):
    def queue(self, n=1):
        self.settings(enabled=1)
        for i in range(n):
            # replies are stamped with the real clock so flush() (also real-clock) sees them as fresh
            self.ingest(f"+25471100{i:04d}", sms(f"CODE{i:06d}XX"), now=time.time())

    def cmds(self):
        return self.q("SELECT * FROM modem_commands WHERE command = 'send_sms' ORDER BY id")

    def test_modem_not_ready_keeps_replies_queued_and_creates_no_commands(self):
        self.modem_ready(False)
        self.queue(2)
        self.assertEqual(replies.flush()["submitted"], 0)
        self.assertEqual((self.cmds(), self.reply_kinds()), ([], ["ack_unassigned"] * 2))

    def test_submit_then_mark_sent_when_the_daemon_completes_the_command(self):
        self.modem_ready()
        self.queue(1)
        self.assertEqual(replies.flush()["submitted"], 1)
        (cmd,) = self.cmds()
        self.assertIn(self.replies()[0][1], cmd["payload_json"])
        db.complete_modem_command(cmd["id"], "done", "ok")
        self.assertEqual(replies.flush()["sent"], 1)
        self.assertEqual(self.q("SELECT status FROM outbound_replies")[0]["status"], "sent")

    def test_inflight_cap_stops_a_burst_from_flooding_the_daemon(self):
        self.modem_ready()
        self.queue(10)
        replies.flush(max_inflight=3)
        self.assertEqual(len(self.cmds()), 3)
        replies.flush(max_inflight=3)
        self.assertEqual(len(self.cmds()), 3, "nothing more until some complete")
        for c in self.cmds():
            db.complete_modem_command(c["id"], "done", "ok")
        replies.flush(max_inflight=3)
        self.assertEqual(len(self.cmds()), 6)

    def test_failed_send_is_retried_a_limited_number_of_times(self):
        self.modem_ready()
        self.queue(1)
        t = time.time()
        for _ in range(12):                                    # the daemon fails every attempt
            replies.flush(now=t, max_attempts=3)
            for c in self.cmds():
                if c["status"] == "pending":
                    db.complete_modem_command(c["id"], "failed", "no network")
            t += 120                                           # past the retry backoff, inside the expiry
        row = self.q("SELECT * FROM outbound_replies")[0]
        self.assertEqual((row["status"], row["attempts"]), ("failed", 3))
        self.assertEqual(len(self.cmds()), 3, "gave up after three attempts, no more commands")
        self.assertIn("no network", row["error"])

    def test_stale_pending_command_is_cancelled_so_it_cannot_fire_hours_later(self):
        self.modem_ready()
        self.queue(1)
        t = time.time()
        replies.flush(now=t)
        (cmd,) = self.cmds()
        counts = replies.flush(now=t + 31 * 60)               # default expiry is 30 min
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(self.q("SELECT status FROM outbound_replies")[0]["status"], "expired")
        self.assertEqual(db.get_modem_command(cmd["id"])["status"], "failed")

    def test_old_queued_reply_expires_instead_of_being_sent_late(self):
        self.modem_ready(False)
        self.queue(1)
        replies.flush(now=time.time() + 31 * 60)
        self.assertEqual(self.q("SELECT status FROM outbound_replies")[0]["status"], "expired")

    def test_template_rendering(self):
        self.assertEqual(replies.render("Ksh {amount} for {nope}", {"amount": "5"}), "Ksh 5 for {nope}")
        self.assertEqual(replies.render("[{region}]", {"region": None}), "[]")
        self.assertEqual(replies.fmt_money(20000), "200")
        self.assertEqual(replies.fmt_money(3105000), "31,050")
        self.assertEqual(replies.fmt_money(150050), "1,500.50")


if __name__ == "__main__":
    unittest.main()
