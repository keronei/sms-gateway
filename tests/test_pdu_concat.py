"""
Tests for modem/pdu.py::decode_deliver_pdu()'s concatenation (UDH) field
extraction - the foundation manager.py's multi-part reassembly builds on.
Uses tests/pdu_fixtures.py to build realistic DELIVER PDUs since pdu.py
itself has no encoder for them (see that module's docstring).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem import pdu
from tests.pdu_fixtures import build_deliver_pdu


class TestConcatDecode(unittest.TestCase):
    def test_single_part_message_has_no_concat_fields(self):
        hex_pdu = build_deliver_pdu("+254712345678", "just a normal message")
        decoded = pdu.decode_deliver_pdu(hex_pdu)
        self.assertIsNone(decoded["concat_ref"])
        self.assertIsNone(decoded["concat_total"])
        self.assertIsNone(decoded["concat_seq"])
        self.assertEqual(decoded["text"], "just a normal message")
        self.assertEqual(decoded["sender"], "+254712345678")

    def test_concat_part_extracts_ref_total_seq(self):
        hex_pdu = build_deliver_pdu("+254712345678", "part two of three", concat=(42, 3, 2))
        decoded = pdu.decode_deliver_pdu(hex_pdu)
        self.assertEqual(decoded["concat_ref"], 42)
        self.assertEqual(decoded["concat_total"], 3)
        self.assertEqual(decoded["concat_seq"], 2)
        self.assertEqual(decoded["text"], "part two of three")

    def test_concat_ucs2_part_round_trips(self):
        # non-GSM7 text forces UCS2 - UDH offset math differs from the
        # GSM7 septet-packed case, worth covering separately
        hex_pdu = build_deliver_pdu("+254712345678", "héllo wörld 你好", concat=(7, 2, 1))
        decoded = pdu.decode_deliver_pdu(hex_pdu)
        self.assertEqual(decoded["concat_ref"], 7)
        self.assertEqual(decoded["concat_total"], 2)
        self.assertEqual(decoded["concat_seq"], 1)
        self.assertEqual(decoded["text"], "héllo wörld 你好")

    def test_concat_ref_wraps_correctly_at_byte_boundary(self):
        # 8-bit reference: valid range is 0-255
        hex_pdu = build_deliver_pdu("+254712345678", "x", concat=(255, 4, 4))
        decoded = pdu.decode_deliver_pdu(hex_pdu)
        self.assertEqual(decoded["concat_ref"], 255)
        self.assertEqual(decoded["concat_seq"], 4)


if __name__ == "__main__":
    unittest.main()
