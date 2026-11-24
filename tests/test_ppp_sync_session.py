"""
The temporary internet "session" the contributions sync uses (modem/ppp.py
request_session()/end_session() and the daemon commands built on them).
Invariants under test: a session never needs/changes the persisted
auto-connect setting, ends cleanly without being treated as a drop or
redialled, and never disconnects a link the user asked for.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from modem import manager as manager_module  # noqa: E402
from tests.db_test_utils import TempDbTestCase  # noqa: E402
from tests.test_manager_ppp_gate import FakePPPSupervisor, PppGateTestCase  # noqa: E402
from tests.test_ppp_supervisor import ScriptedPPPSupervisor  # noqa: E402


class SessionSupervisor(ScriptedPPPSupervisor):
    def _monitor_until_dropped(self):
        self.monitor_calls += 1
        outcome = self.monitor_results.pop(0) if self.monitor_results else "dropped"
        if outcome == "end_session":
            self.end_session()
        elif outcome == "user_reconnect":
            self.request_reconnect()
        elif outcome == "stop":
            self._stop.set()


def make(auto_connect=False):
    return SessionSupervisor(data_port="/dev/ttyFAKE", baud=115200, apn="internet", auto_connect=lambda: auto_connect)


class TestSession(TempDbTestCase):
    def test_session_dials_even_though_auto_connect_is_off(self):
        sup = make(auto_connect=False)
        sup.dial_results, sup.monitor_results, sup.max_iterations = ["10.0.0.9"], ["end_session"], 6
        self.assertEqual(sup.request_session(), "started")
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 1)

    def test_ending_a_session_is_not_a_drop_and_does_not_redial(self):
        sup = make(auto_connect=False)
        sup.dial_results, sup.monitor_results, sup.max_iterations = ["10.0.0.9", "10.0.0.9"], ["end_session"], 8
        sup.request_session()
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 1, "must not dial again after the session ends")
        self.assertFalse(sup._authorized)
        self.assertFalse(sup._session_override)
        self.assertEqual(db.get_modem_status()["ppp_state"], "down")

    def test_without_a_session_the_gate_is_still_closed(self):
        sup = make(auto_connect=False)
        sup.max_iterations = 3
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 0)

    def test_session_then_a_user_request_with_auto_connect_off_still_respects_the_gate(self):
        sup = make(auto_connect=False)
        sup.dial_results, sup.monitor_results, sup.max_iterations = ["10.0.0.9"], ["end_session"], 6
        sup.request_session()
        sup._supervise_loop()
        sup.dial_calls = 0
        sup._iterations = 0
        sup.request_reconnect()
        sup.max_iterations = 3
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 0, "a finished session must not leave the gate open")

    def test_end_session_is_a_noop_for_a_user_requested_link(self):
        sup = make(auto_connect=True)
        sup.request_reconnect()
        self.assertFalse(sup.end_session())
        self.assertTrue(sup._authorized, "the user's link is untouched")
        self.assertEqual(sup.kill_calls, 1, "only request_reconnect's own kill, none from end_session")

    def test_request_session_never_hijacks_a_user_link(self):
        sup = make(auto_connect=True)
        sup.request_reconnect()
        self.assertEqual(sup.request_session(), "already_requested")
        sup._connected = True
        self.assertEqual(sup.request_session(), "already_connected")
        self.assertFalse(sup.end_session())

    def test_a_user_reconnect_during_a_session_takes_ownership(self):
        sup = make(auto_connect=True)
        sup.request_session()
        sup.request_reconnect()
        self.assertFalse(sup._session_override)
        self.assertFalse(sup.end_session(), "the sync job's later disconnect must not kill the user's link")

    def test_ending_a_session_that_is_still_dialing_stops_it(self):
        sup = make(auto_connect=False)
        sup.request_session()
        self.assertTrue(sup.end_session())
        self.assertFalse(sup._authorized)
        sup.max_iterations = 3
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 0)


class SessionFake(FakePPPSupervisor):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sessions, self.ended = 0, 0
        self.session_outcome = "started"

    def request_session(self):
        self.sessions += 1
        return self.session_outcome

    def end_session(self):
        self.ended += 1
        return self.session_outcome == "started"


class TestManagerCommands(PppGateTestCase):
    def setUp(self):
        super().setUp()
        manager_module.PPPSupervisor = SessionFake

    def test_sync_connect_does_not_persist_auto_connect(self):
        outcome = self.mgr._start_sync_session()
        self.assertEqual(outcome, "started")
        self.assertEqual(self.mgr.ppp.sessions, 1)
        self.assertNotIn("modem_auto_connect", self.store.settings, "a one-off session must not flip the persistent setting")
        self.assertFalse(self.mgr.ppp.auto_connect(), "and the normal gate stays closed")

    def test_dashboard_reconnect_still_persists_it(self):
        self.mgr._authorize_and_reconnect_ppp()
        self.assertEqual(self.store.settings.get("modem_auto_connect"), 1)

    def test_disconnect_reports_whether_it_was_sync_owned(self):
        self.assertFalse(self.mgr._end_sync_session(), "no supervisor yet")
        self.mgr._start_sync_session()
        self.assertTrue(self.mgr._end_sync_session())
        self.mgr.ppp.session_outcome = "already_connected"
        self.assertFalse(self.mgr._end_sync_session())


if __name__ == "__main__":
    unittest.main()
