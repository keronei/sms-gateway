"""
Tests for modem_gateway.py - the modem-backend equivalent of gateway.py.
Runs against a real temporary sqlite file (tests/db_test_utils.py) since
send_message() polls db.get_modem_command() directly; a background
thread stands in for the modem daemon actually completing the row.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import modem_gateway  # noqa: E402
from tests.db_test_utils import TempDbTestCase  # noqa: E402


class ModemGatewayTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self._real_poll_interval = modem_gateway.POLL_INTERVAL
        self._real_poll_timeout = modem_gateway.POLL_TIMEOUT
        modem_gateway.POLL_INTERVAL = 0.02
        modem_gateway.POLL_TIMEOUT = 1.0

    def tearDown(self):
        modem_gateway.POLL_INTERVAL = self._real_poll_interval
        modem_gateway.POLL_TIMEOUT = self._real_poll_timeout
        super().tearDown()

    def _start_watcher(self, status, result):
        """Stands in for the modem daemon: waits for a pending
        modem_commands row, then completes it."""
        def worker():
            deadline = time.time() + 1
            while time.time() < deadline:
                pending = db.get_pending_modem_commands()
                if pending:
                    db.complete_modem_command(pending[0]["id"], status, result)
                    return
                time.sleep(0.01)
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t


class TestSendMessage(ModemGatewayTestCase):
    def test_send_returns_message_refs_once_daemon_completes_it(self):
        self._start_watcher("done", '{"message_refs": [55], "parts": 1}')
        resp = modem_gateway.send_message({}, ["+254700000001"], "hello")
        self.assertEqual(resp["state"], "Sent")
        self.assertEqual(resp["message_refs"], [55])
        self.assertIsInstance(resp["id"], int)

    def test_send_raises_gateway_error_on_daemon_failure(self):
        self._start_watcher("failed", "CMS ERROR: 500")
        with self.assertRaises(modem_gateway.GatewayError) as ctx:
            modem_gateway.send_message({}, ["+254700000001"], "hello")
        self.assertIn("CMS ERROR", str(ctx.exception))

    def test_send_times_out_if_daemon_never_picks_it_up(self):
        # no watcher this time - nothing ever completes the command
        with self.assertRaises(modem_gateway.GatewayError) as ctx:
            modem_gateway.send_message({}, ["+254700000001"], "hello")
        self.assertIn("Timed out", str(ctx.exception))

    def test_no_recipient_raises_without_enqueueing_anything(self):
        with self.assertRaises(modem_gateway.GatewayError):
            modem_gateway.send_message({}, [], "hello")
        self.assertEqual(db.get_pending_modem_commands(), [])

    def test_empty_text_raises_without_enqueueing_anything(self):
        with self.assertRaises(modem_gateway.GatewayError):
            modem_gateway.send_message({}, ["+254700000001"], "")
        self.assertEqual(db.get_pending_modem_commands(), [])

    def test_enqueues_with_the_expected_payload_shape(self):
        captured = {}

        def watcher():
            deadline = time.time() + 1
            while time.time() < deadline:
                pending = db.get_pending_modem_commands()
                if pending:
                    captured["cmd"] = pending[0]
                    db.complete_modem_command(pending[0]["id"], "done", '{"message_refs": [1]}')
                    return
                time.sleep(0.01)
        threading.Thread(target=watcher, daemon=True).start()

        modem_gateway.send_message({}, ["+254799999999"], "the message body")
        import json
        payload = json.loads(captured["cmd"]["payload_json"])
        self.assertEqual(payload["phone"], "+254799999999")
        self.assertEqual(payload["text"], "the message body")


class TestConnectionStatus(ModemGatewayTestCase):
    def test_no_status_row_yet(self):
        """After init_db(), a modem_status row already exists (seeded at
        startup) but with device_present=0/at_ready=0 defaults - this is
        what a fresh install looks like before the daemon has ever
        actually detected the modem."""
        result = modem_gateway.test_connection({})
        self.assertFalse(result["ok"])
        self.assertIn("not detected", result["message"])

    def test_fresh_and_ready(self):
        db.update_modem_status(device_present=1, at_ready=1, sim_status="ready")
        result = modem_gateway.test_connection({})
        self.assertTrue(result["ok"])

    def test_stale_status_reported_as_not_running(self):
        db.update_modem_status(device_present=1, at_ready=1, sim_status="ready")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE modem_status SET last_updated = ? WHERE id = 1",
                (time.time() - modem_gateway.STATUS_STALE_SECONDS - 5,),
            )
        result = modem_gateway.test_connection({})
        self.assertFalse(result["ok"])
        self.assertIn("stale", result["message"])

    def test_device_not_present(self):
        db.update_modem_status(device_present=0, at_ready=0, sim_status="unknown")
        result = modem_gateway.test_connection({})
        self.assertFalse(result["ok"])
        self.assertIn("not detected", result["message"])

    def test_sim_not_ready(self):
        db.update_modem_status(device_present=1, at_ready=1, sim_status="pin_required")
        result = modem_gateway.test_connection({})
        self.assertFalse(result["ok"])
        self.assertIn("SIM", result["message"])


if __name__ == "__main__":
    import unittest
    unittest.main()
