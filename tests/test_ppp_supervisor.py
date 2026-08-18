"""
Tests for modem/ppp.py's PPPSupervisor authorization state machine:
- a fresh (or rebuilt) supervisor never dials on its own - only
  request_reconnect() (an explicit ask) opens the gate
- once connected, a real drop (bundle exhausted, signal lost, ...)
  requires a fresh explicit request before trying again - it must NOT
  auto-redial forever
- a deliberate request_reconnect() that interrupts an active connection
  (e.g. settings changed) DOES redial immediately, skipping backoff
- dial failures (never successfully connected) keep retrying via backoff
  while still authorized - that's resilience toward the same request,
  not a new unrequested connection

Runs against a real temporary sqlite file (tests/db_test_utils.py) since
_supervise_loop() calls db.update_modem_status()/db.get_modem_status()
directly. The actual I/O (pppd, chat, reading interface IPs, sleeping) is
replaced with fast, scriptable fakes - only the loop/authorization logic
itself is real.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem.ppp import PPPSupervisor  # noqa: E402
from tests.db_test_utils import TempDbTestCase  # noqa: E402


class ScriptedPPPSupervisor(PPPSupervisor):
    """Real _supervise_loop()/request_reconnect()/_authorized logic (the
    part under test), with the actual I/O swapped for fast, scriptable
    fakes so tests run synchronously and deterministically."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dial_results = []   # queue of IP strings or None, consumed in order by each dial attempt
        self.dial_calls = 0
        self.monitor_results = []  # queue of "dropped" | "reconnect" | "stop"
        self.monitor_calls = 0
        self.wait_calls = []     # records the `seconds` argument of every _wait() call
        self.kill_calls = 0
        self.max_iterations = 20  # safety valve so a buggy test/loop can't hang forever
        self._iterations = 0

    def _dial_and_wait_for_ip(self):
        self.dial_calls += 1
        return self.dial_results.pop(0) if self.dial_results else None

    def _monitor_until_dropped(self):
        self.monitor_calls += 1
        outcome = self.monitor_results.pop(0) if self.monitor_results else "dropped"
        if outcome == "reconnect":
            self._reconnect_now.set()  # simulate a request arriving WHILE connected
        elif outcome == "stop":
            self._stop.set()
        # "dropped": the real connection just died - nothing extra to simulate

    def _kill_pppd(self):
        self.kill_calls += 1

    def _wait(self, seconds):
        self.wait_calls.append(seconds)
        self._reconnect_now.clear()
        self._iterations += 1
        if self._iterations >= self.max_iterations:
            self._stop.set()  # safety valve


def make_supervisor(auto_connect=True):
    return ScriptedPPPSupervisor(
        data_port="/dev/ttyFAKE", baud=115200, apn="internet",
        auto_connect=lambda: auto_connect,
    )


class TestFreshSupervisorNeverAutoDials(TempDbTestCase):
    def test_never_dials_without_an_explicit_request(self):
        sup = make_supervisor(auto_connect=True)  # even with the settings gate open
        sup.max_iterations = 3
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 0, "must never dial on its own")

    def test_stays_down_even_if_auto_connect_setting_is_on(self):
        """The settings gate being on is not, by itself, sufficient - this
        is the actual fix: it used to be."""
        sup = make_supervisor(auto_connect=True)
        sup.max_iterations = 3
        sup._supervise_loop()
        self.assertFalse(sup._authorized)


class TestRequestReconnect(TempDbTestCase):
    def test_authorizes_and_triggers_a_dial(self):
        sup = make_supervisor(auto_connect=True)
        sup.dial_results = [None]  # one failed attempt, then stop via max_iterations
        sup.max_iterations = 2
        sup.request_reconnect()
        self.assertTrue(sup._authorized)
        sup._supervise_loop()
        self.assertGreaterEqual(sup.dial_calls, 1)

    def test_does_not_dial_if_settings_gate_is_off(self):
        """_authorized alone isn't sufficient either - both gates matter."""
        sup = make_supervisor(auto_connect=False)
        sup.max_iterations = 3
        sup.request_reconnect()
        self.assertTrue(sup._authorized)
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 0)


class TestRealDropDeauthorizes(TempDbTestCase):
    def test_successful_connect_then_real_drop_requires_a_fresh_request(self):
        sup = make_supervisor(auto_connect=True)
        sup.dial_results = ["10.0.0.5"]
        sup.monitor_results = ["dropped"]
        sup.max_iterations = 5
        sup.request_reconnect()

        sup._supervise_loop()

        self.assertEqual(sup.dial_calls, 1, "must not redial on its own after a real drop")
        self.assertFalse(sup._authorized, "must require a fresh explicit request after a real drop")

    def test_second_explicit_request_after_a_drop_dials_again(self):
        sup = make_supervisor(auto_connect=True)
        sup.dial_results = ["10.0.0.5"]
        sup.monitor_results = ["dropped"]
        sup.max_iterations = 3
        sup.request_reconnect()
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 1)
        self.assertFalse(sup._authorized)

        # a fresh explicit request (e.g. user clicks Reconnect again after recharging)
        sup.dial_results = ["10.0.0.9"]
        sup.monitor_results = ["dropped"]
        sup._stop.clear()
        sup._iterations = 0
        sup.request_reconnect()
        sup._supervise_loop()
        self.assertEqual(sup.dial_calls, 2, "the new explicit request must trigger a new dial")


class TestDeliberateReconnectWhileConnected(TempDbTestCase):
    def test_redials_immediately_without_backoff_and_stays_authorized(self):
        sup = make_supervisor(auto_connect=True)
        sup.dial_results = ["10.0.0.1", "10.0.0.2"]
        sup.monitor_results = ["reconnect", "stop"]
        sup.max_iterations = 10
        sup.request_reconnect()

        sup._supervise_loop()

        self.assertEqual(sup.dial_calls, 2, "must redial immediately after a deliberate reconnect")
        self.assertTrue(sup._authorized,
                         "must stay authorized - the connection wasn't lost, it was intentionally cycled")


class TestDialFailureKeepsRetryingWhileAuthorized(TempDbTestCase):
    def test_repeated_dial_failures_keep_retrying_via_backoff(self):
        sup = make_supervisor(auto_connect=True)
        sup.dial_results = [None, None, "10.0.0.3"]
        sup.monitor_results = ["stop"]  # once connected, just stop
        sup.max_iterations = 10
        sup.request_reconnect()

        sup._supervise_loop()

        self.assertEqual(sup.dial_calls, 3)
        self.assertTrue(sup._authorized, "dial failures must not deauthorize - still the same request")
        # two backoff waits should have happened (after the two failed attempts)
        self.assertGreaterEqual(len(sup.wait_calls), 2)


class TestAuthorizedFlagDefaultsAndIsolation(TempDbTestCase):
    def test_fresh_supervisor_starts_unauthorized(self):
        sup = make_supervisor()
        self.assertFalse(sup._authorized)

    def test_two_independent_supervisors_do_not_share_state(self):
        sup1 = make_supervisor()
        sup2 = make_supervisor()
        sup1.request_reconnect()
        self.assertTrue(sup1._authorized)
        self.assertFalse(sup2._authorized)


if __name__ == "__main__":
    import unittest
    unittest.main()
