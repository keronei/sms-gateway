"""
Tests for modem/sms.py::list_messages() - that concat_ref/concat_total/
concat_seq survive the AT+CMGL response parsing (_parse_cmgl_pdu), since
manager.py's reassembly logic depends entirely on those fields making it
through this layer unchanged.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem import sms
from tests.fake_modem import FakeATChannel
from tests.pdu_fixtures import build_deliver_pdu, cmgl_lines


class TestListMessagesConcat(unittest.TestCase):
    def test_single_part_message_concat_fields_are_none(self):
        pdu_hex = build_deliver_pdu("+254700000001", "hello")
        lines = cmgl_lines([(1, pdu_hex)]) + ["OK"]
        ch = FakeATChannel(rules=[("AT+CMGL=4", lines)])
        records = sms.list_messages(ch)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["concat_ref"])
        self.assertEqual(records[0]["body"], "hello")
        self.assertEqual(records[0]["sim_index"], 1)

    def test_multipart_entries_each_carry_their_concat_fields(self):
        part1 = build_deliver_pdu("+254700000001", "first ", concat=(9, 2, 1))
        part2 = build_deliver_pdu("+254700000001", "second", concat=(9, 2, 2))
        lines = cmgl_lines([(1, part1), (2, part2)]) + ["OK"]
        ch = FakeATChannel(rules=[("AT+CMGL=4", lines)])
        records = sms.list_messages(ch)
        self.assertEqual(len(records), 2)
        r1, r2 = records
        self.assertEqual((r1["concat_ref"], r1["concat_total"], r1["concat_seq"]), (9, 2, 1))
        self.assertEqual((r2["concat_ref"], r2["concat_total"], r2["concat_seq"]), (9, 2, 2))
        self.assertEqual(r1["sim_index"], 1)
        self.assertEqual(r2["sim_index"], 2)

    def test_mixed_single_and_multipart_entries_in_one_listing(self):
        standalone = build_deliver_pdu("+254700000002", "standalone")
        part = build_deliver_pdu("+254700000001", "only part", concat=(5, 1, 1))
        lines = cmgl_lines([(1, standalone), (2, part)]) + ["OK"]
        ch = FakeATChannel(rules=[("AT+CMGL=4", lines)])
        records = sms.list_messages(ch)
        self.assertIsNone(records[0]["concat_ref"])
        self.assertEqual(records[1]["concat_ref"], 5)


if __name__ == "__main__":
    unittest.main()
