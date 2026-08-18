"""
serial_transport.py - raw termios-based serial I/O for the modem's AT ports.

Based on a custom transport contributed after investigation found that the
MU509's ttyUSB2 interface (a fully AT/SMS/URC-capable control port) doesn't
support the DTR-related modem-control operations pyserial performs as part
of its normal open() sequence, causing pyserial to fail with BrokenPipeError
on that port. Native raw-termios tools (e.g. picocom) work fine because they
never touch those lines. This module talks to the port the same minimal way,
using os/termios/select directly instead of pyserial.

Deliberately avoids anything DTR/modem-control related:
  - no HUPCL (which would toggle modem-control lines on *close*, possibly
    reintroducing the same problem this module exists to avoid)
  - no reliance on pyserial's open-time line-state handling at all
"""
import os
import fcntl
import time
import select
import termios
import threading

# termios only accepts specific symbolic speed constants, not arbitrary
# integers - map the common AT-modem rates we might realistically see.
_BAUD_CONSTANTS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
    460800: getattr(termios, "B460800", termios.B115200),
    921600: getattr(termios, "B921600", termios.B115200),
}

# TIOCEXCL isn't always exposed as a termios constant depending on platform/
# Python build - this is the standard Linux value as a fallback.
_TIOCEXCL = getattr(termios, "TIOCEXCL", 0x540C)


class HuaweiSerial:
    def __init__(self, port, baudrate=115200, timeout=1.0, debug=False):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.debug = debug
        self.fd = None
        self.lock = threading.RLock()

    def open(self):
        if self.fd is not None:
            return
        # O_NONBLOCK on open() is cheap insurance against open() itself
        # hanging on certain line-state conditions; our read() below already
        # gates every os.read() behind select(), so it doesn't change that
        # read behavior at all.
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

        try:
            fcntl.ioctl(self.fd, _TIOCEXCL)
        except OSError:
            # not fatal - some platforms/permission setups don't support
            # this, but we'd rather keep going than fail outright over it
            pass

        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0  # iflag
        attrs[1] = 0  # oflag
        # deliberately NOT including HUPCL - that toggles modem-control
        # lines on close, which is exactly the class of operation this
        # transport exists to avoid
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0  # lflag (raw mode: no canonical/echo/signals)

        baud_const = _BAUD_CONSTANTS.get(self.baudrate, termios.B115200)
        attrs[4] = baud_const  # ispeed
        attrs[5] = baud_const  # ospeed

        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError:
                # already gone (e.g. the device disappeared before we got
                # here) - nothing more we can do, and self.fd is already
                # cleared above either way
                pass

    @property
    def is_open(self):
        return self.fd is not None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()

    def _mark_dead(self):
        """The device physically went away mid-operation (USB unplugged,
        a power blip, the kernel dropping the node, ...) - os.write()/
        os.read()/select() surface this as a plain OSError, commonly
        ENXIO/EIO/ENODEV. Close out our side immediately so is_open
        reflects reality right away rather than leaving a stale fd that
        would make every subsequent call fail in confusing, inconsistent
        ways (or, for a fd number that's since been reused by an
        unrelated file, silently write/read the wrong thing)."""
        self.close()

    def write(self, data):
        with self.lock:
            if self.fd is None:
                raise OSError(f"Serial port {self.port} is not open")
            if isinstance(data, str):
                data = data.encode()
            if self.debug:
                print("TX>", data)
            try:
                os.write(self.fd, data)
            except OSError as e:
                self._mark_dead()
                raise OSError(
                    f"Write to {self.port} failed, port is now closed "
                    f"(device may have disconnected): {e}"
                ) from e

    def flush(self):
        pass  # no userspace write buffering here - os.write() goes straight to the kernel

    def read(self, size=1024):
        if self.fd is None:
            return b""  # not open (never opened, or already marked dead) -
                         # treat like "nothing available" rather than raising,
                         # since callers poll this in a tight loop and a
                         # closed port is an expected, not exceptional, state
        try:
            r, _, _ = select.select([self.fd], [], [], self.timeout)
        except OSError as e:
            self._mark_dead()
            raise OSError(
                f"select() on {self.port} failed, port is now closed "
                f"(device may have disconnected): {e}"
            ) from e
        if not r:
            return b""
        try:
            d = os.read(self.fd, size)
        except BlockingIOError:
            return b""  # possible with O_NONBLOCK in a narrow race right after select()
        except OSError as e:
            self._mark_dead()
            raise OSError(
                f"Read from {self.port} failed, port is now closed "
                f"(device may have disconnected): {e}"
            ) from e
        if not d:
            # select() said the fd was readable, but os.read() came back
            # empty anyway - per POSIX that specifically means EOF (the
            # peer/device is gone for good), NOT "nothing to report this
            # iteration" (that's the `if not r` timeout branch above,
            # already handled). Without this check, a disconnected port
            # that reaches this state would busy-loop forever: select()
            # keeps reporting "ready", read() keeps returning nothing, and
            # nothing ever raises to let _read_loop's except-and-break
            # notice the port is dead.
            self._mark_dead()
            raise OSError(f"{self.port} reached EOF (device disconnected)")
        if self.debug:
            print("RX>", d)
        return d

    def readline(self):
        out = b""
        end = time.time() + self.timeout
        while time.time() < end:
            b = self.read(1)
            if not b:
                continue
            out += b
            if out.endswith(b"\n"):
                break
        return out

    def flush_input(self):
        while self.read(1024):
            pass
