"""
ppp.py - dials the modem's *data* port (separate from the AT control port)
into a PPP internet connection using pppd + chat, and supervises it:
retries with exponential backoff on failure or disconnect, and reports live
status through db.modem_status so the dashboard can display it.

Never touches the AT control port - SMS/USSD keep working on that port
independently of whatever this is doing.

Requires the `ppp` package (`sudo apt install ppp`) - see README.
"""
import os
import re
import shlex
import signal
import subprocess
import threading
import time

import db
from modem.backoff import ExponentialBackoff

RUNTIME_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "modem_runtime"
)
CONNECT_TIMEOUT = 40      # seconds to wait for an IP after launching pppd
MONITOR_INTERVAL = 5      # seconds between liveness checks while connected
PPPD_BIN = "/usr/sbin/pppd"
CHAT_BIN = "/usr/sbin/chat"
PPP_UNIT = 0
PPP_IFACE = f"ppp{PPP_UNIT}"

# From pppd(8)'s EXIT STATUS section - the common ones worth explaining inline
# rather than leaving the user to look up a bare exit code.
PPPD_EXIT_CODES = {
    1: "fatal error",
    2: "options error",
    3: "not setuid-root / insufficient privilege",
    4: "kernel does not support PPP",
    5: "terminated by signal",
    6: "serial port could not be locked",
    7: "serial port could not be opened",
    8: "the connect (chat) script failed - check the AT dial exchange just "
       "above this in the log for the actual reason (e.g. NO CARRIER/ERROR "
       "from the network, often means no active data bundle/APN rejected)",
    10: "PPP negotiation failed - no network protocols could be started",
    11: "peer refused to authenticate",
    16: "the modem hung up",
    17: "serial loopback detected",
    19: "we failed to authenticate ourselves to the peer (check PPP username/password)",
}


class PPPSupervisor:
    def __init__(self, data_port, baud, apn, username="", password="", auto_connect=lambda: True):
        self.data_port = data_port
        self.baud = baud
        self.apn = apn
        self.username = username
        self.password = password
        self.auto_connect = auto_connect

        self._stop = threading.Event()
        self._reconnect_now = threading.Event()
        self._thread = None
        self._proc = None
        self._backoff = ExponentialBackoff(base=5, factor=2, max_delay=300)

        os.makedirs(RUNTIME_DIR, exist_ok=True)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._reconnect_now.set()
        self._kill_pppd()
        if self._thread:
            self._thread.join(timeout=5)

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def request_reconnect(self):
        """Force an immediate reconnect attempt, resetting the backoff counter."""
        self._backoff.reset()
        self._kill_pppd()
        self._reconnect_now.set()

    # ------------------------------------------------------------- loop
    def _supervise_loop(self):
        self._log("info", "PPP supervisor starting")
        while not self._stop.is_set():
            if not self.auto_connect():
                db.update_modem_status(ppp_state="down")
                self._wait(5)
                continue

            db.update_modem_status(ppp_state="dialing")
            self._log("info", f"Dialing on {self.data_port} (APN={self.apn or '(none)'})")
            ip = self._dial_and_wait_for_ip()

            if ip:
                self._backoff.reset()
                db.update_modem_status(
                    ppp_state="connected", ppp_ip=ip, ppp_last_error=None,
                    ppp_retry_count=0, ppp_connected_since=time.time(), ppp_next_retry_at=None,
                )
                self._log("info", f"Connected, IP={ip}")
                self._monitor_until_dropped()
                if self._stop.is_set():
                    break
                self._log("warn", "PPP link dropped")
                db.update_modem_status(ppp_state="down", ppp_ip=None)
            else:
                self._kill_pppd()

            if self._stop.is_set():
                break

            delay = self._backoff.next_delay()
            status = db.get_modem_status()
            db.update_modem_status(
                ppp_state="backoff",
                ppp_retry_count=(status.get("ppp_retry_count") or 0) + 1,
                ppp_next_retry_at=time.time() + delay,
            )
            self._log("warn", f"Retrying in {delay:.0f}s (attempt {self._backoff.attempt})")
            self._wait(delay)

    def _wait(self, seconds):
        """Sleep up to `seconds`, waking early on stop() or request_reconnect()."""
        self._reconnect_now.clear()
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set() or self._reconnect_now.is_set():
                return
            time.sleep(min(0.5, max(0.0, end - time.time())))

    # ------------------------------------------------------------- dialing
    def _ensure_tty_options_file(self):
        """pppd treats options in this per-device file as coming from a
        trusted, root-owned source, unlike the same options passed on the
        command line (which some pppd builds subject to stricter UID
        checks - this is what "using the ... option requires root
        privilege" is about, even when the invoking process genuinely is
        root). Writing here also fails loudly if we're NOT actually root,
        which is itself a useful diagnostic."""
        tty_name = os.path.basename(self.data_port)
        path = f"/etc/ppp/options.{tty_name}"
        content = "defaultroute\nreplacedefaultroute\nusepeerdns\n"
        try:
            os.makedirs("/etc/ppp", exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            os.chmod(path, 0o644)
        except OSError as e:
            self._log("error", f"Could not write {path} ({e}) - is this daemon actually running as root? "
                                f"Falling back to passing routing options on the pppd command line, which may "
                                f"itself fail with 'requires root privilege' if not.")
            return False
        return True

    def _write_chat_script(self):
        path = os.path.join(RUNTIME_DIR, "chat_connect")
        apn = self.apn or "internet"
        lines = [
            "ABORT 'BUSY'",
            "ABORT 'NO CARRIER'",
            "ABORT 'NO DIALTONE'",
            "ABORT 'NO ANSWER'",
            "ABORT 'ERROR'",
            "TIMEOUT 25",
            "'' AT",
            "OK ATE0",
            f'OK AT+CGDCONT=1,"IP","{apn}"',
            "OK ATD*99#",
            "CONNECT ''",
        ]
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _ensure_secrets_files(self):
        """noauth (which we pass regardless) only means 'don't require the
        peer to authenticate to us' - it does NOT stop the network from
        challenging *us* for PAP/CHAP credentials during negotiation, which
        many APNs do even when the actual credentials are trivial/blank.
        Without an entry here, pppd has nothing to respond with and fails
        with 'couldn't find any suitable secret'. Using '*' wildcards for
        server/IP so this matches whatever the network asks for."""
        username = self.username or ""
        password = self.password or ""
        entry = f'"{username}" * "{password}" *\n'
        for fname in ("pap-secrets", "chap-secrets"):
            path = f"/etc/ppp/{fname}"
            try:
                os.makedirs("/etc/ppp", exist_ok=True)
                with open(path, "w") as f:
                    f.write(entry)
                os.chmod(path, 0o600)  # pppd expects these to be root-only-readable
            except OSError as e:
                self._log("error", f"Could not write {path} ({e}) - PPP authentication will likely fail")
                return False
        return True

    def _dial_and_wait_for_ip(self):
        chat_path = self._write_chat_script()
        connect_cmd = f"{CHAT_BIN} -v -s -f {shlex.quote(chat_path)}"
        routing_via_file = self._ensure_tty_options_file()
        self._ensure_secrets_files()

        args = [
            PPPD_BIN, self.data_port, str(self.baud),
            "connect", connect_cmd,
            "noipdefault",
            "novj", "novjccomp", "nobsdcomp", "nodeflate",
            "lcp-echo-interval", "15", "lcp-echo-failure", "3",
            "maxfail", "1", "unit", str(PPP_UNIT), "nodetach",
            "noauth",                    # we don't require the *peer* to authenticate to us
            "user", self.username or "",  # our identity to offer if the network challenges us
        ]
        if not routing_via_file:
            # fallback to the old behaviour if we couldn't write the options
            # file (e.g. not actually root) - may still hit the privilege error
            args += ["defaultroute", "replacedefaultroute", "usepeerdns"]

        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except FileNotFoundError:
            self._log("error", f"{PPPD_BIN} not found - is the 'ppp' package installed? (sudo apt install ppp)")
            return None
        self._proc = proc  # shared handle so _kill_pppd() (possibly called from
                            # another thread, e.g. a manual reconnect) can find it

        threading.Thread(target=self._drain_pppd_output, args=(proc,), daemon=True).start()

        deadline = time.time() + CONNECT_TIMEOUT
        while time.time() < deadline:
            # use the local `proc` reference, not self._proc - a concurrent
            # request_reconnect() can null out self._proc mid-loop from
            # another thread, which would otherwise crash this poll() call
            if proc.poll() is not None:
                code = proc.returncode
                hint = PPPD_EXIT_CODES.get(code, "see pppd(8) EXIT STATUS for what this code means")
                self._log("error", f"pppd exited early (code {code}: {hint})")
                db.update_modem_status(ppp_last_error=f"pppd exited with code {code} ({hint})")
                return None
            ip = self._read_interface_ip(PPP_IFACE)
            if ip:
                return ip
            time.sleep(1)

        self._log("error", f"Timed out waiting for {PPP_IFACE} to come up")
        db.update_modem_status(ppp_last_error="Timed out waiting for an IP address")
        self._kill_pppd()
        return None

    def _drain_pppd_output(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                level = "error" if re.search(r"error|fail|cannot", line, re.I) else "info"
                self._log(level, f"pppd: {line}")
        except Exception:
            pass

    def _monitor_until_dropped(self):
        proc = self._proc
        while not self._stop.is_set() and not self._reconnect_now.is_set():
            time.sleep(MONITOR_INTERVAL)
            if proc is None or proc.poll() is not None:
                return
            if not self._read_interface_ip(PPP_IFACE):
                return
        self._kill_pppd()

    @staticmethod
    def _read_interface_ip(iface):
        try:
            out = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", iface],
                capture_output=True, text=True, timeout=3,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if out.returncode != 0:
            return None
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out.stdout)
        return m.group(1) if m else None

    def _kill_pppd(self):
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _log(self, level, message):
        db.add_modem_event(level, "ppp", message)
