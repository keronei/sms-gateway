"""
Tests for modem/serial_transport.py's HuaweiSerial - specifically that a
physical disconnect mid-operation (raw OSError from the OS) is handled
gracefully: write()/read() raise a clear OSError and immediately mark the
port closed (is_open becomes False right away), rather than leaving a
stale fd that makes every subsequent call fail in confusing ways, hang,
or (worse) silently succeed against a since-reused fd number.

Uses a real pty pair (os.openpty()) rather than mocks - HuaweiSerial calls
termios.tcgetattr/tcsetattr during open(), which only work against an
actual tty device, not a plain file or pipe. Closing the master side of
the pty is what genuinely reproduces the OSError a USB-serial disconnect
would raise on the slave side (confirmed: errno 5, EIO, matching real
hardware).
"""
import os
import pty
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem.serial_transport import HuaweiSerial


class PtyTestCase(unittest.TestCase):
    """Provides a live pty pair and a HuaweiSerial already open()'d against
    the slave side, resembling a real (currently-working) serial port."""

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.slave_path = os.ttyname(self.slave)
        self.ser = HuaweiSerial(self.slave_path, baudrate=115200, timeout=0.3)
        self.ser.open()

    def tearDown(self):
        try:
            self.ser.close()
        except OSError:
            pass
        for fd in (self.master,):
            try:
                os.close(fd)
            except OSError:
                pass

    def disconnect(self):
        """Simulates the device physically going away: close the far end
        of the pty, so the slave side starts raising OSError/EIO on
        read/write/select, the same as a real USB-serial disconnect."""
        os.close(self.master)


class TestNormalOperation(PtyTestCase):
    def test_write_then_read_round_trip(self):
        self.ser.write(b"AT\r")
        os.write(self.master, b"OK\r\n")
        data = self.ser.read(64)
        self.assertEqual(data, b"OK\r\n")

    def test_write_accepts_str_too(self):
        self.ser.write("AT\r")  # str, not bytes
        os.write(self.master, b"OK\r\n")
        self.assertEqual(self.ser.read(64), b"OK\r\n")

    def test_read_returns_empty_bytes_when_nothing_available(self):
        self.assertEqual(self.ser.read(64), b"")

    def test_is_open_true_while_genuinely_open(self):
        self.assertTrue(self.ser.is_open)


class TestDisconnectDuringWrite(PtyTestCase):
    def test_write_raises_oserror_after_disconnect(self):
        self.disconnect()
        with self.assertRaises(OSError):
            self.ser.write(b"AT\r")

    def test_is_open_becomes_false_immediately_after_a_failed_write(self):
        self.disconnect()
        try:
            self.ser.write(b"AT\r")
        except OSError:
            pass
        self.assertFalse(self.ser.is_open, "must reflect the failure immediately, not leave a stale fd")

    def test_subsequent_write_after_disconnect_fails_cleanly_not_confusingly(self):
        """Once marked dead, a second write() must raise a clear 'not
        open' error rather than crashing on a None fd with a confusing
        TypeError, or silently doing nothing."""
        self.disconnect()
        with self.assertRaises(OSError):
            self.ser.write(b"AT\r")
        with self.assertRaises(OSError) as ctx:
            self.ser.write(b"AT\r")
        self.assertIn("not open", str(ctx.exception))


class TestDisconnectDuringRead(PtyTestCase):
    def test_read_raises_oserror_after_disconnect(self):
        self.disconnect()
        with self.assertRaises(OSError):
            self.ser.read(64)

    def test_is_open_becomes_false_immediately_after_a_failed_read(self):
        self.disconnect()
        try:
            self.ser.read(64)
        except OSError:
            pass
        self.assertFalse(self.ser.is_open)

    def test_read_on_a_never_opened_port_returns_empty_not_an_error(self):
        """A closed/never-opened port is a normal, expected state for
        read() (callers poll it in a tight loop) - it should return
        b"", not raise, unlike write()."""
        ser = HuaweiSerial("/dev/does-not-matter", timeout=0.1)
        self.assertEqual(ser.read(64), b"")

    def test_write_on_a_never_opened_port_raises(self):
        ser = HuaweiSerial("/dev/does-not-matter", timeout=0.1)
        with self.assertRaises(OSError):
            ser.write(b"AT\r")


class TestCloseIsIdempotentAndSafe(PtyTestCase):
    def test_double_close_does_not_raise(self):
        self.ser.close()
        self.ser.close()  # must not raise on an already-closed port
        self.assertFalse(self.ser.is_open)

    def test_close_after_disconnect_does_not_raise(self):
        """The fd may already be invalid at the OS level (device gone) -
        close() must swallow that, not propagate a fresh OSError on top
        of the one already raised by write()/read()."""
        self.disconnect()
        try:
            self.ser.write(b"AT\r")
        except OSError:
            pass
        self.ser.close()  # must not raise


class TestProbePortStillHandlesDisconnectGracefully(PtyTestCase):
    """ports.py's probe_port() wraps everything in `except OSError: return
    False` - confirms that contract still holds against the new exception
    behavior (a disconnect mid-probe must not raise past that boundary)."""

    def test_context_manager_use_survives_a_mid_probe_disconnect(self):
        try:
            with self.ser as s:
                self.disconnect()
                s.write(b"AT\r")
                s.read(64)
                result = True
        except OSError:
            result = False
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
