"""
Integration test for ModemManager._handle_send_ussd(): confirms the fix in
modem/ussd.py's UssdWaiter actually holds through the real code path, not
just in isolation - a reply that arrives immediately (simulated here as
arriving while ussd.send() is still "in flight" from the caller's point of
view) must result in a completed command with the correct text, not a
false "No response from the network within 30s" failure.

Runs against a real temporary sqlite file (tests/db_test_utils.py) since
_handle_send_ussd touches db.complete_modem_command/db.update_modem_status.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from modem import manager as manager_module  # noqa: E402
from tests.db_test_utils import TempDbTestCase  # noqa: E402
from tests.fake_modem import FakeATChannel  # noqa: E402
from tests.manager_test_utils import make_manager  # noqa: E402


class TestHandleSendUssdRaceFix(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.mgr = make_manager()

    def test_reply_that_races_in_during_send_is_not_lost(self):
        """Monkeypatches ussd.send() to simulate the real-world race: the
        network's +CUSD: reply gets processed by the reader thread (here,
        synchronously as a side effect) before _handle_send_ussd gets
        around to calling wait_for_reply()."""
        ch = FakeATChannel(rules=[('AT+CUSD=1,"*144#",15', "OK")], default_ok=False)
        self.mgr.control_channel = ch

        real_send = manager_module.ussd.send

        def racing_send(channel, text, timeout=10):
            real_send(channel, text, timeout=timeout)
            # simulate the network reply arriving and being processed by
            # the reader thread right here, before this function returns
            # control to _handle_send_ussd
            self.mgr._on_urc('+CUSD: 0,"Your balance is 120 KES",15')

        manager_module.ussd.send = racing_send
        try:
            cmd_id = db.enqueue_modem_command("send_ussd", {"text": "*144#"})
            cmd = {"id": cmd_id, "payload_json": json.dumps({"text": "*144#"})}
            self.mgr._handle_send_ussd(cmd)
        finally:
            manager_module.ussd.send = real_send

        completed = db.get_modem_command(cmd_id)
        self.assertEqual(completed["status"], "done",
                          "must not report a timeout when the reply actually arrived")
        result = json.loads(completed["result"])
        self.assertEqual(result["text"], "Your balance is 120 KES")
        self.assertEqual(result["session_state"], 0)

    def test_bare_no_text_reply_completes_with_empty_text_not_the_raw_line(self):
        ch = FakeATChannel(rules=[('AT+CUSD=1,"*144*1#",15', "OK")], default_ok=False)
        self.mgr.control_channel = ch

        real_send = manager_module.ussd.send

        def racing_send(channel, text, timeout=10):
            real_send(channel, text, timeout=timeout)
            self.mgr._on_urc("+CUSD: 0")

        manager_module.ussd.send = racing_send
        try:
            cmd_id = db.enqueue_modem_command("send_ussd", {"text": "*144*1#"})
            cmd = {"id": cmd_id, "payload_json": json.dumps({"text": "*144*1#"})}
            self.mgr._handle_send_ussd(cmd)
        finally:
            manager_module.ussd.send = real_send

        completed = db.get_modem_command(cmd_id)
        self.assertEqual(completed["status"], "done")
        result = json.loads(completed["result"])
        self.assertEqual(result["text"], "")
        self.assertNotIn("+CUSD", result["text"])


if __name__ == "__main__":
    import unittest
    unittest.main()
