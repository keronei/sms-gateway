"""
Tests for modem/ussd.py - +CUSD response parsing (CUSD_RE / UssdWaiter)
and the send-then-wait race between a fast network reply and
wait_for_reply() being called.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem import ussd


class TestCusdRegexParsing(unittest.TestCase):
    """Direct regex-level checks, before any buffering/threading is involved."""

    def test_standard_reply_with_text(self):
        m = ussd.CUSD_RE.match('+CUSD: 0,"Thank you for using our service",15')
        self.assertIsNotNone(m)
        state, text, dcs = m.groups()
        self.assertEqual(state, "0")
        self.assertEqual(text, "Thank you for using our service")
        self.assertEqual(dcs, "15")

    def test_reply_needing_further_action(self):
        m = ussd.CUSD_RE.match('+CUSD: 1,"1. Balance\\n2. Bundles",15')
        self.assertEqual(m.group(1), "1")

    def test_bare_reply_with_no_text_at_all(self):
        """The bug: a plain session-end ack with no <str>/<dcs> at all is
        valid per 3GPP TS 27.007 and common in practice."""
        m = ussd.CUSD_RE.match("+CUSD: 0")
        self.assertIsNotNone(m)
        state, text, dcs = m.groups()
        self.assertEqual(state, "0")
        self.assertIsNone(text)
        self.assertIsNone(dcs)

    def test_bare_reply_network_terminated(self):
        m = ussd.CUSD_RE.match("+CUSD: 2")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "2")

    def test_bare_reply_with_no_extra_space(self):
        m = ussd.CUSD_RE.match("+CUSD:0")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "0")

    def test_text_with_embedded_newline(self):
        m = ussd.CUSD_RE.match('+CUSD: 1,"Line one\nLine two\nLine three",15')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "Line one\nLine two\nLine three")

    def test_text_with_stray_embedded_quote_takes_first_to_last(self):
        # simulates a coincidental raw byte value that happens to be '"'
        # inside garbled/packed content - greedy match to the LAST quote
        m = ussd.CUSD_RE.match('+CUSD: 0,"abc"def"ghi",15')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), 'abc"def"ghi')

    def test_dcs_omitted_entirely(self):
        m = ussd.CUSD_RE.match('+CUSD: 0,"hello"')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "hello")
        self.assertIsNone(m.group(3))


class TestUssdWaiterSingleLine(unittest.TestCase):
    def setUp(self):
        self.waiter = ussd.UssdWaiter()

    def test_simple_reply_resolves_immediately(self):
        self.waiter.reset()
        consumed = self.waiter.on_urc_line('+CUSD: 0,"Your balance is 50 KES",15')
        self.assertTrue(consumed)
        result = self.waiter.wait_for_reply(timeout=1)
        self.assertIsNotNone(result)
        self.assertEqual(result["session_state"], 0)
        self.assertEqual(result["text"], "Your balance is 50 KES")
        self.assertEqual(result["dcs"], 15)

    def test_bare_no_text_reply_resolves_immediately_not_via_buffering(self):
        """Before the fix, this fell through to the buffer/settle path and
        came back with the raw "+CUSD: 0" line as the "text"."""
        self.waiter.reset()
        start = time.time()
        self.waiter.on_urc_line("+CUSD: 0")
        result = self.waiter.wait_for_reply(timeout=1)
        elapsed = time.time() - start
        self.assertIsNotNone(result)
        self.assertEqual(result["session_state"], 0)
        self.assertEqual(result["text"], "")
        self.assertLess(elapsed, 0.2, "must not wait for the buffer settle timer")

    def test_further_action_state_is_preserved(self):
        self.waiter.reset()
        self.waiter.on_urc_line('+CUSD: 1,"Reply with your choice",15')
        result = self.waiter.wait_for_reply(timeout=1)
        self.assertEqual(result["session_state"], 1)

    def test_non_cusd_line_is_not_consumed(self):
        self.waiter.reset()
        consumed = self.waiter.on_urc_line("+CREG: 1,5")
        self.assertFalse(consumed)

    def test_hex_ucs2_text_is_decoded(self):
        self.waiter.reset()
        # "Hi" in hex-encoded UCS2 (4 hex digits per UTF-16BE code unit)
        self.waiter.on_urc_line('+CUSD: 0,"00480069",72')
        result = self.waiter.wait_for_reply(timeout=1)
        self.assertEqual(result["text"], "Hi")


class TestUssdWaiterMultiLine(unittest.TestCase):
    def setUp(self):
        self.waiter = ussd.UssdWaiter()
        self._real_settle = ussd.BUFFER_SETTLE_SECONDS
        self._real_max = ussd.MAX_BUFFER_SECONDS
        ussd.BUFFER_SETTLE_SECONDS = 0.05
        ussd.MAX_BUFFER_SECONDS = 0.2

    def tearDown(self):
        ussd.BUFFER_SETTLE_SECONDS = self._real_settle
        ussd.MAX_BUFFER_SECONDS = self._real_max

    def test_reply_split_across_lines_by_embedded_newline_is_reassembled(self):
        self.waiter.reset()
        self.waiter.on_urc_line('+CUSD: 1,"1. Balance')
        self.waiter.on_urc_line('2. Bundles')
        self.waiter.on_urc_line('3. Exit",15')
        result = self.waiter.wait_for_reply(timeout=1)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "1. Balance\n2. Bundles\n3. Exit")
        self.assertEqual(result["session_state"], 1)

    def test_incomplete_buffer_force_flushes_after_max_buffer_seconds(self):
        """A malformed/never-completing multi-line buffer must still
        resolve eventually rather than hang the caller until its own
        30s timeout."""
        self.waiter.reset()
        self.waiter.on_urc_line('+CUSD: 1,"unterminated text with no closing quote')
        result = self.waiter.wait_for_reply(timeout=1)
        self.assertIsNotNone(result, "must force-resolve via MAX_BUFFER_SECONDS rather than hang")


class TestUssdWaiterRaceCondition(unittest.TestCase):
    """The actual bug: a reply that arrives in the window between send()
    returning and wait_for_reply() being called must not be discarded."""

    def setUp(self):
        self.waiter = ussd.UssdWaiter()

    def test_reply_arriving_before_wait_for_reply_is_still_returned(self):
        self.waiter.reset()
        # simulates the reader thread processing the network's reply
        # (arrived fast) BEFORE the caller gets around to calling
        # wait_for_reply() - e.g. while a log line is being written
        self.waiter.on_urc_line('+CUSD: 0,"Immediate reply",15')

        result = self.waiter.wait_for_reply(timeout=1)
        self.assertIsNotNone(result, "a reply that raced in early must not be silently discarded")
        self.assertEqual(result["text"], "Immediate reply")

    def test_reply_arriving_moments_before_wait_for_reply_via_real_thread(self):
        """Same scenario but with an actual background thread and a small
        real delay, closer to the real timing than the synchronous version
        above."""
        self.waiter.reset()

        def deliver_reply():
            time.sleep(0.02)
            self.waiter.on_urc_line('+CUSD: 0,"Threaded reply",15')

        threading.Thread(target=deliver_reply, daemon=True).start()
        result = self.waiter.wait_for_reply(timeout=2)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "Threaded reply")

    def test_reset_discards_a_stale_unclaimed_reply(self):
        """reset() must actually clear old state - a previous reply that
        was never consumed (e.g. an end-session +CUSD: URC nobody waited
        for) must not leak into the next request's wait_for_reply()."""
        self.waiter.reset()
        self.waiter.on_urc_line('+CUSD: 2,"stale, never consumed",15')
        # ... nobody calls wait_for_reply() here - simulating an unconsumed reply

        self.waiter.reset()  # about to send a new request
        result = self.waiter.wait_for_reply(timeout=0.1)
        self.assertIsNone(result, "reset() must discard the stale reply, not let it leak into the next wait")

    def test_timeout_when_no_reply_ever_arrives(self):
        self.waiter.reset()
        start = time.time()
        result = self.waiter.wait_for_reply(timeout=0.15)
        elapsed = time.time() - start
        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 0.15)


if __name__ == "__main__":
    unittest.main()
