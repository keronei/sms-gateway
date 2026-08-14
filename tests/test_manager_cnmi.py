"""
Tests for ModemManager._configure_sms() - that it correctly wires
sms.configure()'s ds fallback result into self._cnmi_ds and logs
appropriately at each fallback level, without needing any real serial
port, GPIO, or sqlite database.

We construct ModemManager() directly (never call .run()) and stub out
.log() to capture calls instead of touching db.add_modem_event(), since
db.init_db()/db.get_settings() aren't relevant to this unit of behavior.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem.manager import ModemManager
from tests.fake_modem import cnmi_channel


class CapturingLogger:
    def __init__(self):
        self.entries = []  # list of (level, category, message)

    def __call__(self, level, category, message):
        self.entries.append((level, category, message))

    def messages(self):
        return [m for _, _, m in self.entries]

    def levels(self):
        return [lvl for lvl, _, _ in self.entries]


def make_manager():
    mgr = ModemManager()
    mgr.log = CapturingLogger()
    return mgr


class TestConfigureSmsDsWiring(unittest.TestCase):
    def test_ds1_success_cached_and_logged_info(self):
        mgr = make_manager()
        ch = cnmi_channel(supported_ds={1, 0})
        mgr._configure_sms(ch)
        self.assertEqual(mgr._cnmi_ds, 1)
        self.assertEqual(mgr.log.levels(), ["info"])
        self.assertIn("1,1,0,1,0", mgr.log.messages()[0])

    def test_ds0_fallback_cached_and_logged_as_warning(self):
        mgr = make_manager()
        ch = cnmi_channel(supported_ds={0})
        mgr._configure_sms(ch)
        self.assertEqual(mgr._cnmi_ds, 0)
        self.assertEqual(mgr.log.levels(), ["warn"])
        msg = mgr.log.messages()[0]
        self.assertIn("1,1,0,0,0", msg)
        self.assertIn("Delivery reports are OFF", msg)

    def test_ds2_via_explicit_order_logs_sr_unsupported_warning(self):
        """ds=2 isn't reachable through the default fallback anymore, but
        _log_cnmi_ds must still handle it sanely if ever set directly
        (e.g. a manual ds_order override) - and should say plainly that
        the reports are lost, not "not implemented yet"."""
        mgr = make_manager()
        mgr._cnmi_ds = 2
        mgr._log_cnmi_ds("PDU mode configured")
        self.assertEqual(mgr.log.levels(), ["warn"])
        msg = mgr.log.messages()[0]
        self.assertIn("SR", msg)
        self.assertIn("never actually be", msg)

    def test_total_failure_without_retry_leaves_ds_none_and_logs_error(self):
        mgr = make_manager()
        ch = cnmi_channel(supported_ds=set())
        mgr._configure_sms(ch, retry_after_settle=False)
        self.assertIsNone(mgr._cnmi_ds)
        self.assertEqual(mgr.log.levels(), ["error"])

    def test_total_failure_with_retry_settle_retries_once_and_can_recover(self):
        """Simulates a SIM that wasn't ready on the first pass (all ds
        rejected transiently) but works by the time the settle-retry
        fires - retry_after_settle=True should pick that up."""
        mgr = make_manager()
        mgr._sleep = lambda seconds: None  # don't actually block the test

        attempts = {"n": 0}

        def flaky_cnmi_response(command):
            attempts["n"] += 1
            # first pass (ds=1, then ds=0) both rejected; second pass
            # (retry) succeeds on its first try, ds=1
            from tests.fake_modem import cms_error
            if attempts["n"] <= 2:
                return cms_error(303)
            return "OK"

        import re
        from tests.fake_modem import FakeATChannel
        ch = FakeATChannel(rules=[
            ("AT+CMGF=0", "OK"),
            (re.compile(r"^AT\+CNMI=1,1,0,\d+,0$"), flaky_cnmi_response),
        ])

        mgr._configure_sms(ch, retry_after_settle=True)
        self.assertEqual(mgr._cnmi_ds, 1)
        # one error from the first pass, one info from the successful retry
        self.assertEqual(mgr.log.levels(), ["error", "info"])
        self.assertIn("retry", mgr.log.messages()[1])

    def test_total_failure_with_retry_settle_still_failing_logs_two_errors(self):
        mgr = make_manager()
        mgr._sleep = lambda seconds: None
        ch = cnmi_channel(supported_ds=set())
        mgr._configure_sms(ch, retry_after_settle=True)
        self.assertIsNone(mgr._cnmi_ds)
        self.assertEqual(mgr.log.levels(), ["error", "error"])


if __name__ == "__main__":
    unittest.main()
