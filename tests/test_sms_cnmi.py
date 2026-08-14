"""
Tests for modem/sms.py::configure() - AT+CNMI configuration.

Per the Huawei MU509 AT Command Interface Spec (section 6.3.3), <mode>=2
is "reserved, not supported currently" on this module - that's what was
actually behind the original "+CMS ERROR: 303", not something specific to
ds=1. <mode>=1 is the module's own documented example for this exact
config (section 6.3.4: "AT+CNMI=1,1,0,1,0"). These tests cover both the
mode fix itself and the ds=1 -> ds=0 fallback that remains as a safety net
(ds=2 is deliberately excluded from the default fallback - see sms.py's
CNMI_DS_FALLBACK docstring: "SR" storage isn't supported on this module
either, so ds=2 would silently lose every report).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem import sms
from modem.serial_at import ATError
from tests.fake_modem import FakeATChannel, cms_error, at_timeout, cnmi_channel


class TestCnmiMode(unittest.TestCase):
    """Regression coverage for the mode=2 -> mode=1 fix itself."""

    def test_uses_mode_1_not_mode_2(self):
        """This is the actual fix: mode=2 is unsupported on this module,
        so configure() must send mode=1 (the module's documented example),
        never mode=2."""
        ch = cnmi_channel(supported_ds={1}, mode=1)
        sms.configure(ch)
        self.assertEqual(ch.sent_commands, ["AT+CMGF=0", "AT+CNMI=1,1,0,1,0"])
        self.assertTrue(all("CNMI=2" not in c for c in ch.sent_commands))

    def test_mode_2_would_have_been_rejected(self):
        """Documents *why* the fix was needed: replaying the exact old
        command against a module that only accepts mode=1 fails, matching
        the +CMS ERROR: 303 originally observed."""
        ch = cnmi_channel(supported_ds={1}, mode=1)  # module only answers mode=1
        with self.assertRaises(AssertionError):
            # the old, buggy command shape - never actually sent by
            # configure() anymore, simulated here directly to show it
            # doesn't match anything this module accepts
            ch.send("AT+CNMI=2,1,0,1,0")


class TestCnmiDsFallback(unittest.TestCase):
    def test_ds1_supported_used_directly(self):
        """The expected/common case now that mode=1 is correct: ds=1
        works first try, only two commands sent."""
        ch = cnmi_channel(supported_ds={1, 0})
        ds = sms.configure(ch)
        self.assertEqual(ds, 1)
        self.assertEqual(ch.sent_commands, ["AT+CMGF=0", "AT+CNMI=1,1,0,1,0"])

    def test_ds1_rejected_falls_back_to_ds0(self):
        """Safety net for the (now unexpected, since mode=1+ds=1 is the
        documented-supported combination) case where ds=1 is still
        rejected for some other reason."""
        ch = cnmi_channel(supported_ds={0})
        ds = sms.configure(ch)
        self.assertEqual(ds, 0)
        self.assertEqual(
            ch.sent_commands,
            ["AT+CMGF=0", "AT+CNMI=1,1,0,1,0", "AT+CNMI=1,1,0,0,0"],
        )

    def test_ds2_is_not_in_the_default_fallback(self):
        """ds=2 relies on "SR" storage, which the spec documents as
        unsupported on this module - it must never be tried by default,
        since it would silently lose every delivery report rather than
        erroring visibly like ds=0 does."""
        ch = cnmi_channel(supported_ds={0, 2})  # only ds=2 or ds=0 would "succeed"
        ds = sms.configure(ch)
        self.assertEqual(ds, 0)
        self.assertNotIn("AT+CNMI=1,1,0,2,0", ch.sent_commands)

    def test_all_default_ds_values_rejected_raises_last_error(self):
        ch = cnmi_channel(supported_ds=set(), ds_error_code=303)
        with self.assertRaises(ATError) as ctx:
            sms.configure(ch)
        self.assertIn("303", str(ctx.exception))
        self.assertEqual(
            ch.sent_commands,
            ["AT+CMGF=0", "AT+CNMI=1,1,0,1,0", "AT+CNMI=1,1,0,0,0"],
        )

    def test_cmgf_failure_is_fatal_and_skips_ds_probing_entirely(self):
        """AT+CMGF=0 failing means PDU mode itself isn't up - no point
        (and no attempt) to probe CNMI ds values in that state."""
        ch = cnmi_channel(supported_ds={1, 0}, cmgf_ok=False)
        with self.assertRaises(ATError):
            sms.configure(ch)
        self.assertEqual(ch.sent_commands, ["AT+CMGF=0"])

    def test_ds_probing_stops_at_first_success_even_if_later_ones_would_work(self):
        """Once ds=1 succeeds we must not also try ds=0 - correctness of
        "first working value wins", not "last working value wins"."""
        ch = cnmi_channel(supported_ds={1, 0})
        sms.configure(ch)
        self.assertNotIn("AT+CNMI=1,1,0,0,0", ch.sent_commands)

    def test_timeout_on_ds1_is_treated_like_rejection_and_falls_back(self):
        """An ATTimeout (module hangs on that command) should fall back
        just like a clean CMS ERROR rejection would."""
        rules = [
            ("AT+CMGF=0", "OK"),
            ("AT+CNMI=1,1,0,1,0", at_timeout("AT+CNMI=1,1,0,1,0")),
            ("AT+CNMI=1,1,0,0,0", "OK"),
        ]
        ch = FakeATChannel(rules=rules)
        ds = sms.configure(ch)
        self.assertEqual(ds, 0)

    def test_custom_ds_order_can_still_opt_into_ds2(self):
        """ds=2 stays usable via an explicit ds_order (e.g. if a future
        firmware revision turns out to support SR storage after all) -
        it's just excluded from the default."""
        ch = cnmi_channel(supported_ds={2})
        ds = sms.configure(ch, ds_order=(2,))
        self.assertEqual(ds, 2)
        self.assertEqual(ch.sent_commands, ["AT+CMGF=0", "AT+CNMI=1,1,0,2,0"])


if __name__ == "__main__":
    unittest.main()
