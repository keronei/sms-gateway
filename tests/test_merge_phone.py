"""
Tests for merge.py::normalize_phone() - in particular the bug where a
local-format number (leading 0, no country code configured) got a "+"
blindly prepended, producing an invalid number like "+0712345678".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import merge  # noqa: E402


class TestNormalizePhoneNoCountryCode(unittest.TestCase):
    """default_country_code="" - the case that was broken."""

    def test_leading_zero_local_number_left_untouched(self):
        digits, valid = merge.normalize_phone("0712345678", "")
        self.assertEqual(digits, "0712345678")
        self.assertTrue(valid)

    def test_leading_zero_never_produces_a_plus_zero_number(self):
        digits, _ = merge.normalize_phone("0712345678", "")
        self.assertFalse(digits.startswith("+0"))

    def test_number_without_leading_zero_or_plus_still_gets_a_plus(self):
        """e.g. "254712345678" - already looks like it has a country code
        typed in, just missing the "+" - this case was never broken."""
        digits, valid = merge.normalize_phone("254712345678", "")
        self.assertEqual(digits, "+254712345678")
        self.assertTrue(valid)

    def test_already_has_plus_is_unaffected(self):
        digits, valid = merge.normalize_phone("+254712345678", "")
        self.assertEqual(digits, "+254712345678")
        self.assertTrue(valid)

    def test_00_prefix_international_dialing_format_is_converted(self):
        digits, valid = merge.normalize_phone("00254712345678", "")
        self.assertEqual(digits, "+254712345678")
        self.assertTrue(valid)

    def test_spaces_and_punctuation_in_raw_input_are_stripped(self):
        digits, valid = merge.normalize_phone("0712 345 678", "")
        self.assertEqual(digits, "0712345678")

        digits, valid = merge.normalize_phone("(0712) 345-678", "")
        self.assertEqual(digits, "0712345678")

    def test_empty_or_none_input(self):
        self.assertEqual(merge.normalize_phone("", ""), ("", False))
        self.assertEqual(merge.normalize_phone(None, ""), ("", False))

    def test_too_short_leading_zero_number_marked_invalid(self):
        digits, valid = merge.normalize_phone("07", "")
        self.assertFalse(valid)


class TestNormalizePhoneWithCountryCode(unittest.TestCase):
    """default_country_code set - unaffected by this fix, checked for
    regression."""

    def test_leading_zero_expands_with_country_code(self):
        digits, valid = merge.normalize_phone("0712345678", "254")
        self.assertEqual(digits, "+254712345678")
        self.assertTrue(valid)

    def test_country_code_with_plus_prefix_in_settings_is_handled(self):
        """The settings field might itself be entered as "+254" instead
        of "254" - either should work the same."""
        digits, valid = merge.normalize_phone("0712345678", "+254")
        self.assertEqual(digits, "+254712345678")

    def test_already_has_plus_ignores_configured_country_code(self):
        digits, valid = merge.normalize_phone("+256712345678", "254")
        self.assertEqual(digits, "+256712345678")

    def test_no_leading_zero_still_gets_country_code_prepended(self):
        """A number typed without the leading 0 (e.g. "712345678") should
        still expand correctly - lstrip("0") on a string with no leading
        zeros is a no-op, not a bug."""
        digits, valid = merge.normalize_phone("712345678", "254")
        self.assertEqual(digits, "+254712345678")

    def test_multiple_leading_zeros_all_stripped(self):
        """Edge case: lstrip("0") removes ALL leading zeros, not just
        one - matters for a genuinely unusual local number with a
        repeated leading 0 in the raw CSV data."""
        digits, valid = merge.normalize_phone("00712345678", "254")
        # the 00-prefix branch fires first (international dialing format),
        # consuming both leading zeros before the country-code branch ever
        # sees the string
        self.assertEqual(digits, "+712345678")


if __name__ == "__main__":
    unittest.main()
