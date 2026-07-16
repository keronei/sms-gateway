"""
serial_at.py - a small, generic Hayes AT command engine.

Owns one serial port. A background reader thread continuously reads lines
and routes each one of two ways:
  - if a command is currently awaiting its response, the line is appended to
    that response (unless it looks like a well-known unsolicited code, in
    which case it's diverted to the URC callback even mid-command - see
    FORCE_URC_PREFIXES);
  - otherwise, it's an unsolicited result code (URC) and is handed to
    urc_callback(line).

This module knows nothing about SMS/PPP/USSD/the database - it's a generic,
reusable transport layer. sms.py / ussd.py (added in later milestones) will
build on top of it.
"""
import re
import threading
import serial

# URCs that can plausibly interleave with a command's response and must
# never be swallowed into that response, even if we're mid-command when
# they arrive. NOTE: +CREG:/+CGREG: are deliberately NOT here - they're only
# genuinely unsolicited if unsolicited reporting has been enabled via
# AT+CREG=1/2, which this codebase never does (registration is polled
# on-demand via AT+CREG? instead), so any "+CREG:" line we see is always the
# direct answer to that query. If a later milestone enables unsolicited
# registration reporting, this will need smarter disambiguation (e.g. by
# param count) rather than a blanket prefix match.
FORCE_URC_PREFIXES = (
    "+CMTI:", "+CDS:", "+CMT:",
    "^SYSSTART", "^MODE:", "^BOOT:", "^SIMST:", "^HCSQ:", "RING",
)

TERMINAL_RE = re.compile(r"^(OK|ERROR|\+CME ERROR|\+CMS ERROR)")


class ATError(Exception):
    def __init__(self, message, lines=None):
        super().__init__(message)
        self.lines = lines or []


class ATTimeout(ATError):
    pass


class AtResponse:
    __slots__ = ("ok", "lines", "raw")

    def __init__(self, ok, lines, raw):
        self.ok = ok
        self.lines = lines   # intermediate info lines, echo/terminal stripped
        self.raw = raw

    @property
    def text(self):
        return "\n".join(self.lines)

    def __repr__(self):
        return f"<AtResponse ok={self.ok} lines={self.lines!r}>"


class ATChannel:
    def __init__(self, port, baudrate=115200, read_timeout=0.2, urc_callback=None):
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self.urc_callback = urc_callback or (lambda line: None)

        self._ser = None
        self._reader_thread = None
        self._stop = threading.Event()

        self._cmd_lock = threading.RLock()
        self._pending = False
        self._last_cmd = ""
        self._resp_lines = []
        self._resp_event = threading.Event()
        self._prompt_event = threading.Event()
        self._buf = b""

    @property
    def is_open(self):
        return self._ser is not None and self._ser.is_open

    def open(self):
        self._ser = serial.Serial(self.port, self.baudrate, timeout=self.read_timeout)
        self._stop.clear()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def close(self):
        self._stop.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    # ------------------------------------------------------------- reader
    def _read_loop(self):
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except (serial.SerialException, OSError, TypeError):
                break
            if not chunk:
                continue
            self._buf += chunk
            self._drain_lines()

    def _drain_lines(self):
        # AT+CMGS / AT+CUSD leave a bare '>' prompt open with no CRLF terminator.
        if self._pending and not self._prompt_event.is_set() and self._buf.endswith(b"> "):
            self._prompt_event.set()
            self._buf = b""
            return
        while b"\r\n" in self._buf:
            line, self._buf = self._buf.split(b"\r\n", 1)
            text = line.decode(errors="replace").strip()
            if text:
                self._handle_line(text)

    def _handle_line(self, text):
        is_forced_urc = any(text.startswith(p) for p in FORCE_URC_PREFIXES)
        if self._pending and not is_forced_urc:
            self._resp_lines.append(text)
            if TERMINAL_RE.match(text):
                self._resp_event.set()
        else:
            try:
                self.urc_callback(text)
            except Exception:
                pass

    # ------------------------------------------------------------ commands
    def send(self, command, timeout=10):
        """Send a plain AT command and wait for OK/ERROR. Raises ATError/ATTimeout."""
        with self._cmd_lock:
            self._prepare(command)
            try:
                self._write(command + "\r")
                if not self._resp_event.wait(timeout):
                    raise ATTimeout(f"Timed out waiting for response to {command!r}")
                return self._finalize()
            finally:
                self._pending = False

    def send_expect_prompt(self, command, timeout=10):
        """Send a command that leaves a '>' prompt open (e.g. AT+CMGS=..., AT+CUSD).
        Caller must follow up with send_payload()."""
        with self._cmd_lock:
            self._prepare(command)
            self._write(command + "\r")
            if not self._prompt_event.wait(timeout):
                self._pending = False
                raise ATTimeout(f"Timed out waiting for '>' prompt after {command!r}")
            # deliberately leave _pending True / lock held conceptually until send_payload;
            # caller is expected to call send_payload next.

    def send_payload(self, payload, ctrl_z=True, timeout=20):
        """Send the payload (PDU hex or text) following a '>' prompt, terminated
        by Ctrl+Z. Must be called after send_expect_prompt()."""
        with self._cmd_lock:
            try:
                self._resp_lines = []
                self._resp_event.clear()
                data = payload if isinstance(payload, bytes) else payload.encode()
                if ctrl_z:
                    data += b"\x1a"
                self._ser.write(data)
                self._ser.flush()
                if not self._resp_event.wait(timeout):
                    raise ATTimeout("Timed out waiting for response after payload")
                return self._finalize()
            finally:
                self._pending = False

    def ping(self, timeout=3):
        """Cheap liveness check. Returns True/False instead of raising."""
        try:
            self.send("AT", timeout=timeout)
            return True
        except ATError:
            return False

    # ------------------------------------------------------------ helpers
    def _prepare(self, command):
        self._last_cmd = command.strip()
        self._resp_lines = []
        self._resp_event.clear()
        self._prompt_event.clear()
        self._pending = True

    def _finalize(self):
        lines = self._resp_lines[:]
        # drop a leading echo of the command itself, if local echo is on
        if lines and lines[0] == self._last_cmd:
            lines = lines[1:]
        terminal = lines[-1] if lines else ""
        info_lines = lines[:-1] if lines else []
        if terminal != "OK":
            raise ATError(f"{self._last_cmd!r} failed: {terminal or '(no response)'}", lines=info_lines)
        return AtResponse(ok=True, lines=info_lines, raw="\n".join(self._resp_lines))

    def _write(self, text):
        self._ser.write(text.encode())
        self._ser.flush()
