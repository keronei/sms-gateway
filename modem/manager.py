"""
manager.py - entry point for the standalone modem-manager daemon.

Owns: the AT control port, GPIO power control, and the PPP internet
supervisor. Talks to the rest of the system exclusively through the shared
SQLite database (db.py) - this process is never imported by the Flask app.

Run via: python3 -m modem.manager   (or the run_modem_manager.py wrapper,
which is what the systemd unit actually invokes).
"""
import os
import sys
import time
import json
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402
from modem import gpio_power, ports, sms, ussd  # noqa: E402
from modem.backoff import ExponentialBackoff  # noqa: E402
from modem.serial_at import ATChannel, ATError, ATTimeout  # noqa: E402
from modem.ppp import PPPSupervisor  # noqa: E402

AT_HEALTH_INTERVAL = 20     # seconds between liveness pings once the device is up
NETWORK_INFO_INTERVAL = 60  # seconds between signal/registration refreshes
POWER_ON_SETTLE = 12        # seconds to let the module boot/enumerate USB after a power pulse
COMMAND_POLL_INTERVAL = 1.5  # seconds between checks of modem_commands
INBOX_FALLBACK_POLL = 20    # seconds between inbox drains even without a +CMTI URC


class ModemManager:
    def __init__(self):
        self.settings = {}
        self.control_channel = None
        self.ppp = None
        self._stop = threading.Event()
        self._power_backoff = ExponentialBackoff(base=5, factor=2, max_delay=180)
        self._last_network_refresh = 0
        self._inbox_dirty = threading.Event()
        self._ussd_waiter = ussd.UssdWaiter()
        self._ppp_config_snapshot = None

    # ------------------------------------------------------------- utils
    def reload_settings(self):
        self.settings = db.get_settings()

    def log(self, level, category, message):
        db.add_modem_event(level, category, message)
        print(f"[{level.upper()}] {category}: {message}", flush=True)

    def _sleep(self, seconds):
        self._stop.wait(seconds)

    # -------------------------------------------------------------- run
    def run(self):
        db.init_db()
        self.reload_settings()
        self.log("info", "system", "Modem manager starting")

        threading.Thread(target=self._command_poll_loop, daemon=True).start()
        threading.Thread(target=self._inbox_poll_loop, daemon=True).start()

        while not self._stop.is_set():
            self.reload_settings()
            control_port = self.settings.get("modem_control_port") or "/dev/ttyUSB0"

            if not self._ensure_device_present(control_port):
                delay = self._power_backoff.next_delay()
                db.update_modem_status(device_present=False, at_ready=False, ppp_state="down",
                                        ppp_last_error="modem not responding")
                self.log("warn", "power", f"Modem not responding on {control_port}; retrying in {delay:.0f}s")
                self._sleep(delay)
                continue

            self._power_backoff.reset()
            try:
                self._ensure_control_channel(control_port)
            except (ATError, ATTimeout, OSError) as e:
                self.log("error", "at", f"Failed to open/initialize control channel: {e}")
                self._teardown_control_channel()
                self._sleep(5)
                continue

            self._ensure_ppp_supervisor()
            self._health_check_loop()   # returns when the control port stops responding

        self._shutdown()

    def stop(self):
        self._stop.set()

    def _shutdown(self):
        self.log("info", "system", "Modem manager stopping")
        if self.ppp:
            self.ppp.stop()
        self._teardown_control_channel()
        gpio_power.release()

    # ------------------------------------------------------- presence/power
    def _ensure_device_present(self, control_port):
        if ports.probe_port(control_port):
            db.update_modem_status(device_present=True, control_port=control_port)
            return True

        pin = int(self.settings.get("modem_gpio_power_pin") or gpio_power.DEFAULT_PIN)
        self.log("warn", "power", f"{control_port} not responding; pulsing PWRKEY on GPIO{pin}")
        db.update_modem_status(device_present=False, at_ready=False)
        try:
            gpio_power.power_pulse(pin)
        except Exception as e:
            self.log("error", "power", f"GPIO power pulse failed: {e}")
            return False

        status = db.get_modem_status()
        db.update_modem_status(power_cycle_count=(status.get("power_cycle_count") or 0) + 1)
        self.log("info", "power", f"Power pulse sent; waiting {POWER_ON_SETTLE}s for the module to boot")
        self._sleep(POWER_ON_SETTLE)
        if self._stop.is_set():
            return False
        present = ports.probe_port(control_port)
        db.update_modem_status(device_present=present)
        return present

    # ------------------------------------------------------- control channel
    def _ensure_control_channel(self, control_port):
        if self.control_channel and self.control_channel.is_open and self.control_channel.port == control_port:
            return
        self._teardown_control_channel()
        baud = int(self.settings.get("modem_baud") or 115200)
        self.control_channel = ATChannel(control_port, baudrate=baud, urc_callback=self._on_urc)
        self.control_channel.open()
        self._initialize_modem()

    def _teardown_control_channel(self):
        if self.control_channel:
            try:
                self.control_channel.close()
            except Exception:
                pass
            self.control_channel = None

    def _initialize_modem(self):
        ch = self.control_channel
        ch.send("ATE0")           # echo off - cleaner parsing on our side
        ch.send("AT+CMEE=2")      # verbose +CME/+CMS error strings instead of bare numbers

        try:
            ch.send('AT+CSCS="IRA"')
            self.log("info", "at", "Character set set to IRA (plain ASCII pass-through)")
        except (ATError, ATTimeout) as e:
            # Not fatal, but SMS/USSD text will likely come back hex-encoded
            # instead of readable without this - see text_codec.py's fallback.
            self.log("warn", "at", f"AT+CSCS=\"IRA\" failed (continuing anyway): {e}")

        sim_status = self._check_and_unlock_sim(ch)

        db.update_modem_status(at_ready=True, sim_status=sim_status)
        self.log("info", "at", f"Control channel ready on {ch.port}; SIM status: {sim_status}")

        try:
            sms.configure(ch)
            self.log("info", "sms", "Text mode configured (AT+CMGF=1, AT+CNMI=2,1,0,0,0)")
        except (ATError, ATTimeout) as e:
            self.log("error", "sms", f"Failed to configure SMS text mode: {e}")

        try:
            ch.send("AT^USSDMODE=0")
            self.log("info", "ussd", "USSD non-transparent mode set")
        except (ATError, ATTimeout) as e:
            self.log("warn", "ussd", f"AT^USSDMODE=0 failed (continuing anyway): {e}")

        self._refresh_network_info()
        self._inbox_dirty.set()  # sweep for anything already sitting on the SIM

    def _check_and_unlock_sim(self, ch):
        """Checks AT+CPIN? and, if the SIM is locked, attempts to unlock it
        with whatever PIN is currently configured. Shared by init and by the
        periodic retry in _apply_live_settings_changes(), so entering a PIN
        in Settings after the SIM already came up locked actually gets
        applied without needing a full daemon/channel restart."""
        try:
            resp = ch.send("AT+CPIN?")
            text = resp.text
        except ATError as e:
            self.log("error", "sim", f"AT+CPIN? failed: {e}")
            return "absent"

        if "READY" in text:
            return "ready"
        if "SIM PIN" not in text:
            return "error"

        pin = self.settings.get("modem_sim_pin")
        if not pin:
            self.log("warn", "sim", "SIM requires a PIN but none is configured in Settings")
            return "pin_required"
        try:
            ch.send(f'AT+CPIN="{pin}"')
            self.log("info", "sim", "SIM PIN accepted")
            return "ready"
        except ATError as e:
            self.log("error", "sim", f"SIM PIN rejected: {e}")
            return "pin_error"

    # -------------------------------------------------------------- ppp
    def _ppp_config(self):
        return (
            self.settings.get("modem_data_port") or "/dev/ttyUSB1",
            int(self.settings.get("modem_baud") or 115200),
            self.settings.get("modem_apn") or "",
            self.settings.get("modem_ppp_username") or "",
            self.settings.get("modem_ppp_password") or "",
        )

    def _ensure_ppp_supervisor(self):
        if self.ppp and self.ppp.is_alive():
            return
        data_port, baud, apn, username, password = self._ppp_config()
        self.ppp = PPPSupervisor(
            data_port=data_port, baud=baud, apn=apn, username=username, password=password,
            auto_connect=lambda: bool(self.reload_settings_get("modem_auto_connect", 1)),
        )
        self._ppp_config_snapshot = (data_port, baud, apn, username, password)
        self.ppp.start()

    def reload_settings_get(self, key, default=None):
        # cheap fresh read so toggling "auto connect" in Settings takes effect
        # without waiting for the outer loop's next full reload
        return db.get_settings().get(key, default)

    # ------------------------------------------------------- health / info
    def _health_check_loop(self):
        while not self._stop.is_set():
            self._sleep(AT_HEALTH_INTERVAL)
            if self._stop.is_set():
                return
            if not self.control_channel.ping(timeout=3):
                self.log("error", "at", "Control port stopped responding; will attempt recovery")
                db.update_modem_status(at_ready=False, device_present=False)
                self._teardown_control_channel()
                return
            self.reload_settings()
            if self._apply_live_settings_changes():
                return  # control port changed - let the outer loop reopen it
            if time.time() - self._last_network_refresh > NETWORK_INFO_INTERVAL:
                self._refresh_network_info()

    def _apply_live_settings_changes(self):
        """Picks up Settings edits made while this component is already
        running, without requiring a full daemon restart. Returns True if
        the control channel was torn down (caller must stop using it and
        let the outer loop in run() reopen it on the new port)."""
        control_port = self.settings.get("modem_control_port") or "/dev/ttyUSB0"
        if self.control_channel and control_port != self.control_channel.port:
            self.log("info", "system",
                     f"Control port changed in Settings ({self.control_channel.port} -> {control_port}); reopening")
            self._teardown_control_channel()
            return True

        if self.ppp and self._ppp_config() != self._ppp_config_snapshot:
            self.log("info", "ppp", "PPP settings changed; restarting PPP supervisor with the new config")
            self.ppp.stop()
            self.ppp = None
            self._ensure_ppp_supervisor()

        status = db.get_modem_status()
        if status.get("sim_status") in ("pin_required", "pin_error") and self.settings.get("modem_sim_pin"):
            new_status = self._check_and_unlock_sim(self.control_channel)
            if new_status != status.get("sim_status"):
                db.update_modem_status(sim_status=new_status)

        return False

    def _refresh_network_info(self):
        ch = self.control_channel
        updates = {}
        try:
            resp = ch.send("AT+CSQ")
            m = resp.text.strip()
            if m.startswith("+CSQ:"):
                rssi = int(m.split(":", 1)[1].split(",")[0].strip())
                updates["signal_quality"] = rssi
        except (ATError, ATTimeout, ValueError):
            pass
        try:
            resp = ch.send("AT+CREG?")
            m = resp.text.strip()
            if m.startswith("+CREG:"):
                parts = [p.strip() for p in m.split(":", 1)[1].split(",")]
                code = parts[1] if len(parts) > 1 else parts[0]
                updates["network_reg_status"] = {
                    "0": "not_registered", "1": "registered_home", "2": "searching",
                    "3": "denied", "4": "unknown", "5": "registered_roaming",
                }.get(code, code)
        except (ATError, ATTimeout, ValueError, IndexError):
            pass
        try:
            resp = ch.send("AT+COPS?")
            m = resp.text.strip()
            if '"' in m:
                updates["operator"] = m.split('"')[1]
        except (ATError, ATTimeout, IndexError):
            pass
        if updates:
            db.update_modem_status(**updates)
        self._last_network_refresh = time.time()

    def _on_urc(self, line):
        self.log("info", "urc", line)
        if self._ussd_waiter.on_urc_line(line):
            return
        if line.startswith("+CMTI:"):
            # Do NOT call ch.send() here - this callback runs on the reader
            # thread itself, and send() waits on an event that same thread
            # sets; calling it from here would deadlock. Just flag it.
            self._inbox_dirty.set()

    # --------------------------------------------------------------- inbox
    def _inbox_poll_loop(self):
        """Runs independently of the main state machine. Wakes up on a +CMTI
        URC (via _inbox_dirty) or every INBOX_FALLBACK_POLL seconds regardless,
        as a safety net in case a URC gets missed."""
        while not self._stop.is_set():
            woke_on_urc = self._inbox_dirty.wait(INBOX_FALLBACK_POLL)
            self._inbox_dirty.clear()
            if self._stop.is_set():
                return
            ch = self.control_channel
            if not ch or not ch.is_open:
                continue
            try:
                self._drain_inbox(ch)
            except Exception as e:
                self.log("error", "sms", f"Inbox drain failed: {e}")
            if not woke_on_urc:
                continue  # was just the fallback timer firing with nothing to do

    def _drain_inbox(self, ch):
        try:
            records = sms.list_messages(ch)
        except (ATError, ATTimeout) as e:
            self.log("error", "sms", f"AT+CMGL failed: {e}")
            return
        if not records:
            return
        for rec in records:
            received_at = rec["received_at"] if rec["received_at"] is not None else time.time()
            db.add_modem_inbox_message(
                sender=rec["sender"], body=rec["body"], raw_timestamp=rec["raw_timestamp"],
                received_at=received_at, sim_index=rec["sim_index"],
            )
            self.log("info", "sms", f"New message from {rec['sender']} copied to inbox (SIM slot {rec['sim_index']})")
            try:
                sms.delete_message(ch, rec["sim_index"])
            except (ATError, ATTimeout) as e:
                self.log("error", "sms", f"Failed to clear SIM slot {rec['sim_index']} after copying: {e}")

    # --------------------------------------------------------- commands
    def _command_poll_loop(self):
        """Runs independently of the main state machine so UI actions (power
        cycle / reconnect / SMS / USSD) are picked up promptly regardless of
        what the main loop is currently doing. Each command runs on its own
        short-lived thread so a slow one (USSD can wait up to its timeout for
        a network reply) never blocks the others queued behind it - the
        underlying ATChannel already serializes actual serial I/O safely."""
        while not self._stop.is_set():
            try:
                for cmd in db.get_pending_modem_commands():
                    db.claim_modem_command(cmd["id"])
                    threading.Thread(target=self._handle_command, args=(cmd,), daemon=True).start()
            except Exception as e:
                self.log("error", "system", f"Command poll error: {e}")
            self._stop.wait(COMMAND_POLL_INTERVAL)

    def _handle_command(self, cmd):
        name = cmd["command"]
        try:
            if name == "power_cycle":
                pin = int(self.settings.get("modem_gpio_power_pin") or gpio_power.DEFAULT_PIN)
                self.log("info", "power", "Manual power-cycle requested from dashboard")
                # NOTE: if an SMS/USSD command is currently mid-wait on this same
                # control channel, tearing it down here closes the reader thread
                # that wait depends on - that in-flight command will simply time
                # out on its own (30s for USSD) rather than deadlock or corrupt
                # anything. Rare in practice (you'd have to manually power-cycle
                # while something else is actively waiting on a reply) and
                # self-healing, so not worth more machinery for this milestone.
                self._teardown_control_channel()
                gpio_power.power_pulse(pin)
                self._power_backoff.reset()
                db.complete_modem_command(cmd["id"], "done", "Power pulse sent")
            elif name == "reconnect_ppp":
                self.log("info", "ppp", "Manual reconnect requested from dashboard")
                self.reload_settings()
                if self.ppp and self._ppp_config() != self._ppp_config_snapshot:
                    self.log("info", "ppp", "PPP settings changed; rebuilding supervisor before reconnecting")
                    self.ppp.stop()
                    self.ppp = None
                    self._ensure_ppp_supervisor()
                elif self.ppp:
                    self.ppp.request_reconnect()
                else:
                    self._ensure_ppp_supervisor()
                db.complete_modem_command(cmd["id"], "done", "Reconnect triggered")
            elif name == "send_sms":
                self._handle_send_sms(cmd)
            elif name == "send_ussd":
                self._handle_send_ussd(cmd)
            elif name == "end_ussd_session":
                self._handle_end_ussd_session(cmd)
            else:
                db.complete_modem_command(cmd["id"], "failed", f"Unknown command: {name}")
        except Exception as e:
            db.complete_modem_command(cmd["id"], "failed", str(e))

    def _handle_send_sms(self, cmd):
        payload = json.loads(cmd["payload_json"] or "{}")
        phone = (payload.get("phone") or "").strip()
        text = payload.get("text") or ""
        ch = self.control_channel
        if not ch or not ch.is_open:
            db.complete_modem_command(cmd["id"], "failed", "Modem control channel is not ready")
            return
        if not phone or not text:
            db.complete_modem_command(cmd["id"], "failed", "Phone and text are required")
            return
        try:
            mr = sms.send_text(ch, phone, text)
            self.log("info", "sms", f"Sent SMS to {phone} (mr={mr})")
            db.complete_modem_command(cmd["id"], "done", json.dumps({"message_ref": mr}))
        except (ATError, ATTimeout) as e:
            self.log("error", "sms", f"Failed to send SMS to {phone}: {e}")
            db.complete_modem_command(cmd["id"], "failed", str(e))

    def _handle_send_ussd(self, cmd):
        payload = json.loads(cmd["payload_json"] or "{}")
        text = (payload.get("text") or "").strip()
        ch = self.control_channel
        if not ch or not ch.is_open:
            db.complete_modem_command(cmd["id"], "failed", "Modem control channel is not ready")
            return
        if not text:
            db.complete_modem_command(cmd["id"], "failed", "USSD text is required")
            return
        try:
            ussd.send(ch, text)
        except (ATError, ATTimeout) as e:
            self.log("error", "ussd", f"AT+CUSD rejected: {e}")
            db.complete_modem_command(cmd["id"], "failed", f"Modem rejected the request: {e}")
            return

        self.log("info", "ussd", f"Sent USSD {text!r}; waiting for network reply")
        reply = self._ussd_waiter.wait_for_reply(timeout=30)
        if reply is None:
            self.log("warn", "ussd", "No USSD reply within 30s")
            db.complete_modem_command(cmd["id"], "failed",
                                       "No response from the network within 30s (the request was sent)")
            return

        active = reply["session_state"] == 1
        db.update_modem_status(ussd_active=active, ussd_last_message=reply["text"],
                                ussd_last_state=reply["session_state"], ussd_updated_at=time.time())
        self.log("info", "ussd", f"USSD reply (state={reply['session_state']}): {reply['text']!r}")
        db.complete_modem_command(cmd["id"], "done", json.dumps(reply))

    def _handle_end_ussd_session(self, cmd):
        ch = self.control_channel
        if not ch or not ch.is_open:
            db.complete_modem_command(cmd["id"], "failed", "Modem control channel is not ready")
            return
        try:
            ussd.end_session(ch)
            db.update_modem_status(ussd_active=False, ussd_updated_at=time.time())
            self.log("info", "ussd", "USSD session ended")
            db.complete_modem_command(cmd["id"], "done", None)
        except (ATError, ATTimeout) as e:
            self.log("error", "ussd", f"Failed to end USSD session: {e}")
            db.complete_modem_command(cmd["id"], "failed", str(e))


def main():
    mgr = ModemManager()
    try:
        mgr.run()
    except KeyboardInterrupt:
        mgr.stop()


if __name__ == "__main__":
    main()
