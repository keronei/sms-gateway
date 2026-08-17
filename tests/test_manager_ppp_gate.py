"""
Tests for the internet-connect gate: the daemon must never dial PPP on its
own just because it started or the control channel came back up - only
because the user explicitly asked, via the dashboard's "Reconnect
internet now" action or by texting "connect" to the SIM. Both of those
paths persist modem_auto_connect=1 (the same setting Settings -> Modem's
checkbox controls), which is what actually opens PPPSupervisor's own
dial gate - see modem/ppp.py's _supervise_loop().

None of this touches a real serial port, subprocess, or sqlite file:
PPPSupervisor is replaced with a recording fake, and db.get_settings/
save_settings/complete_modem_command/add_modem_inbox_message are
monkeypatched onto a tiny in-memory store.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem import manager as manager_module  # noqa: E402
from tests.manager_test_utils import make_manager  # noqa: E402
from tests.pdu_fixtures import build_deliver_pdu  # noqa: E402


class FakePPPSupervisor:
    """Records what would have happened instead of actually touching
    pppd/subprocess/threads."""

    def __init__(self, data_port, baud, apn, username="", password="", auto_connect=lambda: True):
        self.data_port = data_port
        self.baud = baud
        self.apn = apn
        self.username = username
        self.password = password
        self.auto_connect = auto_connect
        self.started = False
        self.stopped = False
        self.reconnect_requested = 0
        self._alive = False

    def start(self):
        self.started = True
        self._alive = True

    def stop(self):
        self.stopped = True
        self._alive = False

    def is_alive(self):
        return self._alive

    def request_reconnect(self):
        self.reconnect_requested += 1


class FakeSettingsStore:
    def __init__(self, initial=None):
        self.settings = dict(initial or {})

    def get_settings(self):
        return dict(self.settings)

    def save_settings(self, data):
        self.settings.update(data)
        return dict(self.settings)


class PppGateTestCase(unittest.TestCase):
    def setUp(self):
        self.mgr = make_manager()
        self.store = FakeSettingsStore()  # empty - mirrors a fresh install
        self.inserted = []  # captured db.add_modem_inbox_message calls

        self._real_get_settings = manager_module.db.get_settings
        self._real_save_settings = manager_module.db.save_settings
        self._real_complete_command = manager_module.db.complete_modem_command
        self._real_add_inbox = manager_module.db.add_modem_inbox_message
        self._real_ppp_cls = manager_module.PPPSupervisor

        manager_module.db.get_settings = self.store.get_settings
        manager_module.db.save_settings = self.store.save_settings
        manager_module.db.complete_modem_command = lambda *a, **kw: None
        manager_module.db.add_modem_inbox_message = self._record_inbox
        manager_module.PPPSupervisor = FakePPPSupervisor

        self.mgr.reload_settings()  # picks up the (empty) fake store

    def tearDown(self):
        manager_module.db.get_settings = self._real_get_settings
        manager_module.db.save_settings = self._real_save_settings
        manager_module.db.complete_modem_command = self._real_complete_command
        manager_module.db.add_modem_inbox_message = self._real_add_inbox
        manager_module.PPPSupervisor = self._real_ppp_cls

    def _record_inbox(self, sender, body, raw_timestamp, received_at, sim_index=None):
        self.inserted.append(dict(sender=sender, body=body))


class TestNoAutoDialOnStartup(PppGateTestCase):
    def test_ensure_ppp_supervisor_starts_but_dial_gate_is_closed_by_default(self):
        """This is the actual fix: _ensure_ppp_supervisor() (called
        unconditionally on every control-channel bring-up in run()) must
        still start the supervisor "thread" (harmless bookkeeping/status
        object), but the gate it's given must be closed until someone
        explicitly authorizes it."""
        self.mgr._ensure_ppp_supervisor()
        self.assertTrue(self.mgr.ppp.started)
        self.assertFalse(self.mgr.ppp.auto_connect(), "must not be authorized to dial by default")

    def test_gate_stays_closed_across_a_simulated_reconnect_with_no_explicit_authorization(self):
        """Simulates the control channel bouncing (as in run()'s main
        loop) without ever going through the dashboard/SMS trigger -
        re-calling _ensure_ppp_supervisor() must not itself open the gate."""
        self.mgr._ensure_ppp_supervisor()
        first = self.mgr.ppp
        first._alive = True  # pretend it's already running, like a real one would be
        self.mgr._ensure_ppp_supervisor()  # is_alive() short-circuits, no rebuild
        self.assertIs(self.mgr.ppp, first)
        self.assertFalse(self.mgr.ppp.auto_connect())


class TestDashboardAuthorizes(PppGateTestCase):
    def test_authorize_and_reconnect_persists_setting_and_builds_supervisor(self):
        self.assertIsNone(self.mgr.ppp)
        self.mgr._authorize_and_reconnect_ppp()
        self.assertEqual(self.store.settings.get("modem_auto_connect"), 1)
        self.assertIsNotNone(self.mgr.ppp)
        self.assertTrue(self.mgr.ppp.started)
        self.assertTrue(self.mgr.ppp.auto_connect(), "gate must now be open")

    def test_authorize_and_reconnect_kicks_existing_alive_supervisor_with_unchanged_config(self):
        self.mgr._ensure_ppp_supervisor()
        self.mgr.ppp._alive = True
        existing = self.mgr.ppp

        self.mgr._authorize_and_reconnect_ppp()

        self.assertIs(self.mgr.ppp, existing, "should reuse the same supervisor, not rebuild it")
        self.assertEqual(self.mgr.ppp.reconnect_requested, 1)
        self.assertEqual(self.store.settings.get("modem_auto_connect"), 1)

    def test_authorize_and_reconnect_rebuilds_supervisor_if_config_changed_meanwhile(self):
        self.mgr._ensure_ppp_supervisor()
        self.mgr.ppp._alive = True
        old = self.mgr.ppp

        self.store.settings["modem_apn"] = "changed-apn"  # simulate a Settings save in between

        self.mgr._authorize_and_reconnect_ppp()

        self.assertTrue(old.stopped)
        self.assertIsNot(self.mgr.ppp, old)
        self.assertTrue(self.mgr.ppp.started)

    def test_reconnect_ppp_command_goes_through_the_same_authorize_path(self):
        self.mgr._handle_command({"id": 1, "command": "reconnect_ppp", "payload": None})
        self.assertEqual(self.store.settings.get("modem_auto_connect"), 1)
        self.assertIsNotNone(self.mgr.ppp)
        self.assertTrue(self.mgr.ppp.auto_connect())


class TestConnectSms(PppGateTestCase):
    def test_exact_connect_message_authorizes_and_reconnects(self):
        self.mgr._maybe_handle_connect_sms("+254700000001", "connect")
        self.assertEqual(self.store.settings.get("modem_auto_connect"), 1)
        self.assertIsNotNone(self.mgr.ppp)
        self.assertTrue(self.mgr.ppp.auto_connect())

    def test_matching_is_case_insensitive_and_trims_whitespace(self):
        self.mgr._maybe_handle_connect_sms("+254700000001", "  ConNect  ")
        self.assertEqual(self.store.settings.get("modem_auto_connect"), 1)

    def test_unrelated_message_body_does_not_authorize(self):
        self.mgr._maybe_handle_connect_sms("+254700000001", "hey are you free later?")
        self.assertNotIn("modem_auto_connect", self.store.settings)
        self.assertIsNone(self.mgr.ppp)

    def test_message_that_merely_contains_the_word_does_not_authorize(self):
        """Must be an exact match, not a substring - "connect me please"
        or similar should not accidentally trigger this."""
        self.mgr._maybe_handle_connect_sms("+254700000001", "please connect me to support")
        self.assertNotIn("modem_auto_connect", self.store.settings)
        self.assertIsNone(self.mgr.ppp)

    def test_incoming_sms_delivery_pipeline_triggers_connect(self):
        """End-to-end through _drain_inbox()/_deliver_single_message(),
        not just the helper in isolation."""
        pdu_hex = build_deliver_pdu("+254700000001", "connect")
        from tests.fake_modem import FakeATChannel
        from tests.pdu_fixtures import cmgl_lines
        import re

        deleted = []

        def cmgd(command):
            deleted.append(int(re.match(r"AT\+CMGD=(\d+)", command).group(1)))
            return "OK"

        ch = FakeATChannel(rules=[
            ("AT+CMGL=4", cmgl_lines([(1, pdu_hex)]) + ["OK"]),
            (re.compile(r"^AT\+CMGD=\d+$"), cmgd),
        ])

        self.mgr._drain_inbox(ch)

        self.assertEqual(len(self.inserted), 1)
        self.assertEqual(self.store.settings.get("modem_auto_connect"), 1)
        self.assertIsNotNone(self.mgr.ppp)
        self.assertTrue(self.mgr.ppp.auto_connect())
        self.assertEqual(deleted, [1])  # still delivered/cleared normally


if __name__ == "__main__":
    unittest.main()
