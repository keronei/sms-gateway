"""
Tests for ModemManager's multi-part (concatenated) incoming SMS
reassembly: _drain_inbox / _buffer_concat_part / _flush_completed_concat_sets
/ _flush_stale_concat_sets / _deliver_concat_set.

We construct ModemManager() directly, stub .log(), and monkeypatch
db.add_modem_inbox_message() to capture calls instead of touching
sqlite - these tests are about the buffering/ordering/timeout state
machine, not the database layer (which db.py's own usage elsewhere
already exercises against a real connection).
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from modem import manager as manager_module  # noqa: E402
from tests.fake_modem import FakeATChannel  # noqa: E402
from tests.manager_test_utils import make_manager  # noqa: E402
from tests.pdu_fixtures import build_deliver_pdu, cmgl_lines  # noqa: E402


class RecordingDb:
    """Captures add_modem_inbox_message() calls instead of hitting sqlite."""

    def __init__(self):
        self.inserted = []  # list of kwargs dicts, insertion order

    def add_modem_inbox_message(self, sender, body, raw_timestamp, received_at, sim_index=None):
        self.inserted.append(dict(sender=sender, body=body, raw_timestamp=raw_timestamp,
                                   received_at=received_at, sim_index=sim_index))
        return len(self.inserted)


def cmgl_channel_sequence(responses):
    """Builds a FakeATChannel where AT+CMGL=4 returns a different
    response (a `cmgl_lines(...) + ["OK"]` list) on each successive call,
    the last response repeating once the list is exhausted (models a
    SIM that's settled - nothing new, nothing changes). AT+CMGD=<n>
    always succeeds and is recorded in `.deleted`.
    """
    call_count = {"n": 0}
    deleted = []

    def cmgl_response(_command):
        idx = min(call_count["n"], len(responses) - 1)
        call_count["n"] += 1
        return responses[idx]

    def cmgd_response(command):
        m = re.match(r"^AT\+CMGD=(\d+)$", command)
        deleted.append(int(m.group(1)))
        return "OK"

    ch = FakeATChannel(rules=[
        ("AT+CMGL=4", cmgl_response),
        (re.compile(r"^AT\+CMGD=\d+$"), cmgd_response),
    ])
    ch.deleted = deleted
    return ch


class ReassemblyTestCase(unittest.TestCase):
    def setUp(self):
        self.mgr = make_manager()
        self.fake_db = RecordingDb()
        self._real_add = db.add_modem_inbox_message
        manager_module.db.add_modem_inbox_message = self.fake_db.add_modem_inbox_message
        # freeze time control for staleness tests
        self._real_time = manager_module.time.time
        self._now = [1_000_000.0]
        manager_module.time.time = lambda: self._now[0]

    def tearDown(self):
        manager_module.db.add_modem_inbox_message = self._real_add
        manager_module.time.time = self._real_time

    def advance(self, seconds):
        self._now[0] += seconds


class TestSinglePartUnaffected(ReassemblyTestCase):
    def test_single_part_message_still_delivered_and_deleted_immediately(self):
        """Regression check: non-concatenated messages must behave exactly
        as before this feature existed."""
        pdu_hex = build_deliver_pdu("+254700000001", "hello there")
        ch = cmgl_channel_sequence([cmgl_lines([(1, pdu_hex)]) + ["OK"]])
        self.mgr._drain_inbox(ch)
        self.assertEqual(len(self.fake_db.inserted), 1)
        self.assertEqual(self.fake_db.inserted[0]["body"], "hello there")
        self.assertEqual(self.fake_db.inserted[0]["sim_index"], 1)
        self.assertEqual(ch.deleted, [1])
        self.assertEqual(self.mgr._concat_buffer, {})


class TestSameDrainReassembly(ReassemblyTestCase):
    def test_two_parts_present_in_one_listing_reassemble_immediately(self):
        part1 = build_deliver_pdu("+254700000001", "Hello, ", concat=(11, 2, 1))
        part2 = build_deliver_pdu("+254700000001", "world!", concat=(11, 2, 2))
        ch = cmgl_channel_sequence([cmgl_lines([(1, part1), (2, part2)]) + ["OK"]])
        self.mgr._drain_inbox(ch)

        self.assertEqual(len(self.fake_db.inserted), 1)
        self.assertEqual(self.fake_db.inserted[0]["body"], "Hello, world!")
        self.assertIsNone(self.fake_db.inserted[0]["sim_index"])
        self.assertEqual(sorted(ch.deleted), [1, 2])
        self.assertEqual(self.mgr._concat_buffer, {})

    def test_three_parts_reassemble_in_order_regardless_of_listing_order(self):
        p1 = build_deliver_pdu("+254700000001", "one-", concat=(3, 3, 1))
        p2 = build_deliver_pdu("+254700000001", "two-", concat=(3, 3, 2))
        p3 = build_deliver_pdu("+254700000001", "three", concat=(3, 3, 3))
        # deliberately out of order: seq 2, then seq 3, then seq 1
        ch = cmgl_channel_sequence([cmgl_lines([(1, p2), (2, p3), (3, p1)]) + ["OK"]])
        self.mgr._drain_inbox(ch)

        self.assertEqual(len(self.fake_db.inserted), 1)
        self.assertEqual(self.fake_db.inserted[0]["body"], "one-two-three")
        self.assertEqual(sorted(ch.deleted), [1, 2, 3])


class TestCrossDrainReassembly(ReassemblyTestCase):
    def test_part_arriving_on_a_later_drain_completes_the_set(self):
        part1 = build_deliver_pdu("+254700000001", "first half, ", concat=(20, 2, 1))
        part2 = build_deliver_pdu("+254700000001", "second half", concat=(20, 2, 2))
        # first drain: only part 1 is on the SIM. second drain: part 1 is
        # STILL there (we never delete an incomplete part) plus the new
        # part 2 - this is what a real SIM would show.
        responses = [
            cmgl_lines([(1, part1)]) + ["OK"],
            cmgl_lines([(1, part1), (2, part2)]) + ["OK"],
        ]
        ch = cmgl_channel_sequence(responses)

        self.mgr._drain_inbox(ch)
        self.assertEqual(len(self.fake_db.inserted), 0, "must not deliver on partial receipt")
        self.assertEqual(ch.deleted, [], "must not delete a part until its set is complete")
        self.assertEqual(len(self.mgr._concat_buffer), 1)

        self.mgr._drain_inbox(ch)
        self.assertEqual(len(self.fake_db.inserted), 1)
        self.assertEqual(self.fake_db.inserted[0]["body"], "first half, second half")
        self.assertEqual(sorted(ch.deleted), [1, 2])
        self.assertEqual(self.mgr._concat_buffer, {})

    def test_repeated_listing_of_the_same_unfinished_part_is_not_double_counted(self):
        """The same still-undeleted part shows up on every poll while its
        set is incomplete - re-seeing it must be a no-op, not grow state
        or attempt redundant work."""
        part1 = build_deliver_pdu("+254700000001", "only part so far", concat=(1, 2, 1))
        ch = cmgl_channel_sequence([cmgl_lines([(1, part1)]) + ["OK"]])

        self.mgr._drain_inbox(ch)
        self.mgr._drain_inbox(ch)
        self.mgr._drain_inbox(ch)

        self.assertEqual(len(self.fake_db.inserted), 0)
        self.assertEqual(ch.deleted, [])
        key = ("+254700000001", 1, 2)
        self.assertEqual(len(self.mgr._concat_buffer[key]["parts"]), 1)


class TestStaleTimeout(ReassemblyTestCase):
    def test_incomplete_set_is_delivered_with_placeholder_after_timeout(self):
        part1 = build_deliver_pdu("+254700000001", "the only part that ever arrives",
                                   concat=(77, 2, 1))
        ch = cmgl_channel_sequence([cmgl_lines([(1, part1)]) + ["OK"]])

        self.mgr._drain_inbox(ch)
        self.assertEqual(len(self.fake_db.inserted), 0)

        self.advance(manager_module.CONCAT_STALE_SECONDS + 1)
        # no new mail on the SIM this time - just the timeout sweep running
        self.mgr._drain_inbox(ch)

        self.assertEqual(len(self.fake_db.inserted), 1)
        body = self.fake_db.inserted[0]["body"]
        self.assertIn("the only part that ever arrives", body)
        self.assertIn("missing", body.lower())
        self.assertIn("2/2", body)
        self.assertEqual(ch.deleted, [1])
        self.assertEqual(self.mgr._concat_buffer, {})

    def test_not_yet_stale_set_is_left_buffered(self):
        part1 = build_deliver_pdu("+254700000001", "part one", concat=(5, 2, 1))
        ch = cmgl_channel_sequence([cmgl_lines([(1, part1)]) + ["OK"]])

        self.mgr._drain_inbox(ch)
        self.advance(manager_module.CONCAT_STALE_SECONDS - 5)
        self.mgr._drain_inbox(ch)

        self.assertEqual(len(self.fake_db.inserted), 0)
        self.assertEqual(len(self.mgr._concat_buffer), 1)


class TestMalformedConcatHeader(ReassemblyTestCase):
    def test_seq_greater_than_total_is_delivered_standalone_not_buffered_forever(self):
        bad = build_deliver_pdu("+254700000001", "corrupt header", concat=(1, 2, 5))  # seq > total
        ch = cmgl_channel_sequence([cmgl_lines([(1, bad)]) + ["OK"]])
        self.mgr._drain_inbox(ch)

        self.assertEqual(len(self.fake_db.inserted), 1)
        self.assertEqual(self.fake_db.inserted[0]["body"], "corrupt header")
        self.assertEqual(self.fake_db.inserted[0]["sim_index"], 1)
        self.assertEqual(ch.deleted, [1])
        self.assertEqual(self.mgr._concat_buffer, {})


if __name__ == "__main__":
    unittest.main()
