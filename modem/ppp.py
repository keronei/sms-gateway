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

    def _dial_and_wait_for_ip(self):
        chat_path = self._write_chat_script()
        connect_cmd = f"{CHAT_BIN} -v -f {shlex.quote(chat_path)}"

        args = [
            PPPD_BIN, self.data_port, str(self.baud),
            "connect", connect_cmd,
            "noipdefault", "defaultroute", "replacedefaultroute", "usepeerdns",
            "novj", "novjccomp", "nobsdcomp", "nodeflate",
            "lcp-echo-interval", "15", "lcp-echo-failure", "3",
            "maxfail", "1", "unit", str(PPP_UNIT), "nodetach",
        ]
        args += ["user", self.username] if self.username else []
        args += ["password", self.password] if self.password else (["noauth"] if not self.username else [])

        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except FileNotFoundError:
            self._log("error", f"{PPPD_BIN} not found - is the 'ppp' package installed? (sudo apt install ppp)")
            return None

        threading.Thread(target=self._drain_pppd_output, args=(self._proc,), daemon=True).start()

        deadline = time.time() + CONNECT_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self._log("error", f"pppd exited early (code {self._proc.returncode})")
                db.update_modem_status(ppp_last_error=f"pppd exited with code {self._proc.returncode}")
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
        while not self._stop.is_set() and not self._reconnect_now.is_set():
            time.sleep(MONITOR_INTERVAL)
            if self._proc is None or self._proc.poll() is not None:
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
