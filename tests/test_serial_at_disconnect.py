"""
Integration test: ATChannel (the class manager.py actually uses) recovers
cleanly from a physical modem disconnect, using a real pty pair rather than
mocks - proves the fix holds through the actual class, not just the lower
serial_transport.py layer in isolation (see tests/test_serial_transport.py).
"""
import os
import pty
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem.serial_at import ATChannel, ATError


class TestATChannelSurvivesDisconnect(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.slave_path = os.ttyname(self.slave)
        self.urcs = []
        self.ch = ATChannel(self.slave_path, read_timeout=0.1, urc_callback=self.urcs.append)
        self.ch.open()

    def tearDown(self):
        self.ch.close()
        try:
            os.close(self.master)
        except OSError:
            pass

    def _respond_ok_once(self):
        """Stands in for the modem: reads whatever command arrives, then
        writes back a plain OK."""
        def worker():
            try:
                os.read(self.master, 256)
                os.write(self.master, b"OK\r\n")
            except OSError:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def test_normal_command_works_before_disconnect(self):
        self._respond_ok_once()
        resp = self.ch.send("AT", timeout=2)
        self.assertTrue(resp.ok)

    def test_write_after_disconnect_raises_aterror_not_a_hang_or_raw_oserror(self):
        os.close(self.master)  # simulate the modem vanishing
        start = time.time()
        with self.assertRaises(ATError):
            self.ch.send("AT", timeout=5)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0, "must fail fast on a dead port, not wait out the full timeout")

    def test_is_open_reflects_disconnect_after_reader_thread_notices(self):
        """The reader thread's own read loop should also notice the port
        is gone (via a read failure) and stop, marking is_open False even
        without a send() ever being attempted."""
        os.close(self.master)
        # give the reader thread's poll cycle (read_timeout=0.1s) a moment
        # to notice and mark the transport dead
        deadline = time.time() + 2
        while self.ch.is_open and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.ch.is_open)

    def test_pending_flag_does_not_leak_after_a_failed_send_expect_prompt(self):
        """Regression test for the _pending leak this fix also closed:
        before it, a write failure inside send_expect_prompt() left
        _pending stuck True forever, silently breaking URC delivery for
        the rest of the channel's life."""
        os.close(self.master)
        with self.assertRaises(ATError):
            self.ch.send_expect_prompt('AT+CMGS="+254700000001"', timeout=2)
        self.assertFalse(self.ch._pending, "_pending must be reset even when the write itself fails")


if __name__ == "__main__":
    unittest.main()
