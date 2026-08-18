"""
manager.py - entry point for the standalone modem-manager daemon.

Owns: the AT control port, GPIO power control, and the PPP internet
supervisor. Talks to the rest of the system exclusively through the shared
SQLite database (db.py) - this process is never imported by the Flask app.

Run via: python3 -m modem.manager   (or the run_modem_manager.py wrapper,
which is what the systemd unit actually invokes).
"""
import os
import re
import sys
import time
import json
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402
from modem import gpio_power, ports, sms, ussd, pdu  # noqa: E402
from modem.backoff import ExponentialBackoff  # noqa: E402
from modem.serial_at import ATChannel, ATError, ATTimeout  # noqa: E402
from modem.ppp import PPPSupervisor  # noqa: E402

AT_HEALTH_INTERVAL = 20     # seconds between liveness pings once the device is up
NETWORK_INFO_INTERVAL = 60  # seconds between signal/registration refreshes
POWER_ON_SETTLE = 12        # seconds to let the module boot/enumerate USB after a power pulse
COMMAND_POLL_INTERVAL = 1.5  # seconds between checks of modem_commands
INBOX_FALLBACK_POLL = 20    # seconds between inbox drains even without a +CMTI URC
CONCAT_STALE_SECONDS = 300  # how long to hold an incomplete multi-part SMS waiting
                             # for its missing part(s) before delivering what we have
CALL_GAP_SECONDS = 10        # RING/+CLIP within this long of the last one = same call
SIM_SETTLE_SECONDS = 2        # wait after a PIN unlock before touching SMS/USSD/CLIP
HEALTH_CHECK_RETRIES = 3      # ping attempts before declaring the control port dead
HEALTH_CHECK_RETRY_GAP = 3    # seconds between those retries
CONNECT_SMS_KEYWORD = "connect"  # inbound SMS body (case-insensitive, trimmed) that
                                  # authorizes + kicks off a PPP connection attempt


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
        self._current_call_id = None
        self._current_call_last_ring = 0.0
        self._pending_cds_header = None   # set while awaiting the PDU-hex line after a +CDS: header
        self._sms_reference_counter = 0   # rotating concat reference for multi-part SMS (0-255)
        self._cnmi_ds = None              # which AT+CNMI ds value the module actually accepted (see sms.configure)
        # Multi-part (concatenated) incoming SMS reassembly buffer.
        # Keyed by (sender, concat_ref, concat_total). Value:
        #   {"parts": {seq: record}, "sim_indexes": {seq: sim_index}, "first_seen": epoch float}
        # Only ever touched from the inbox-poll thread, so no lock needed.
        self._concat_buffer = {}

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

        # unlock the SIM FIRST - AT+CSCS and the SMS/USSD/CLIP setup below are
        # all guaranteed to fail while the SIM is PIN-locked, so there's no
        # point attempting them before this
        sim_status, just_unlocked = self._check_and_unlock_sim(ch)
        db.update_modem_status(at_ready=True, sim_status=sim_status)
        self.log("info", "at", f"Control channel ready on {ch.port}; SIM status: {sim_status}")

        if just_unlocked:
            # many modules need a moment to finish mounting the SIM (storage,
            # phonebook, etc.) right after a PIN is accepted - issuing SMS/
            # USSD-related commands too soon commonly fails with a generic
            # CMS/CME error that has nothing to do with the command itself
            self.log("info", "sim", f"Waiting {SIM_SETTLE_SECONDS}s for the SIM to settle after PIN unlock")
            self._sleep(SIM_SETTLE_SECONDS)

        self._set_charset(ch)
        self._configure_sms(ch, retry_after_settle=just_unlocked)

        try:
            ch.send("AT^USSDMODE=0")
            self.log("info", "ussd", "USSD non-transparent mode set")
        except (ATError, ATTimeout) as e:
            self.log("warn", "ussd", f"AT^USSDMODE=0 failed (continuing anyway): {e}")

        try:
            ch.send("AT+CLIP=1")
            self.log("info", "call", "Caller ID presentation enabled (AT+CLIP=1)")
        except (ATError, ATTimeout) as e:
            self.log("warn", "call", f"AT+CLIP=1 failed - incoming calls will show without a number: {e}")

        self._refresh_network_info()
        self._inbox_dirty.set()  # sweep for anything already sitting on the SIM

    def _set_charset(self, ch):
        try:
            ch.send('AT+CSCS="IRA"')
            self.log("info", "at", "Character set set to IRA (plain ASCII pass-through)")
        except (ATError, ATTimeout) as e:
            # Not fatal, but SMS/USSD text will likely come back hex-encoded
            # instead of readable without this - see text_codec.py's fallback.
            self.log("warn", "at", f"AT+CSCS=\"IRA\" failed (continuing anyway): {e}")

    def _configure_sms(self, ch, retry_after_settle=False):
        try:
            self._cnmi_ds = sms.configure(ch)
            self._log_cnmi_ds("PDU mode configured")
            return
        except (ATError, ATTimeout) as e:
            self._cnmi_ds = None
            self.log("error", "sms", f"Failed to configure SMS PDU mode (all CNMI ds values rejected): {e}")
        if not retry_after_settle:
            return
        # one retry with a longer wait, in case the SIM needed more than the
        # initial settle window
        self._sleep(2)
        try:
            self._cnmi_ds = sms.configure(ch)
            self._log_cnmi_ds("PDU mode configured on retry")
        except (ATError, ATTimeout) as e:
            self._cnmi_ds = None
            self.log("error", "sms", f"SMS PDU mode retry also failed: {e}")

    def _log_cnmi_ds(self, prefix):
        ds = self._cnmi_ds
        if ds == 1:
            self.log("info", "sms", f"{prefix} (AT+CMGF=0, AT+CNMI=1,1,0,1,0 - direct delivery reports)")
        elif ds == 2:
            self.log("warn", "sms",
                     f"{prefix} (AT+CMGF=0, AT+CNMI=1,1,0,2,0 - ds=1 was rejected, falling back to "
                     "store+notify via +CDSI). Per the MU509 AT command spec, \"SR\" storage isn't "
                     "supported on this module, so those +CDSI notifications can never actually be "
                     "read back - delivery status will silently never update. Treat this the same as "
                     "ds=0 in practice. Sending/receiving SMS itself is unaffected.")
        elif ds == 0:
            self.log("warn", "sms",
                     f"{prefix} (AT+CMGF=0, AT+CNMI=1,1,0,0,0 - ds=1 was rejected by the module). "
                     "Delivery reports are OFF: sent messages will stay 'sent' and never move to "
                     "'delivered'/'failed'. Sending/receiving SMS itself is unaffected.")
        else:
            self.log("info", "sms", f"{prefix} (ds={ds})")

    def _check_and_unlock_sim(self, ch):
        """Checks AT+CPIN? and, if the SIM is locked, attempts to unlock it
        with whatever PIN is currently configured. Shared by init and by the
        periodic retry in _apply_live_settings_changes(), so entering a PIN
        in Settings after the SIM already came up locked actually gets
        applied without needing a full daemon/channel restart.

        Returns (status, just_unlocked) - just_unlocked is True only when a
        PIN was actually submitted and accepted on THIS call, so callers can
        apply a settle delay only when it's actually needed (a SIM that was
        already ready needs no extra wait)."""
        try:
            resp = ch.send("AT+CPIN?")
            text = resp.text
        except ATError as e:
            self.log("error", "sim", f"AT+CPIN? failed: {e}")
            return "absent", False

        if "READY" in text:
            return "ready", False
        if "SIM PIN" not in text:
            return "error", False

        pin = self.settings.get("modem_sim_pin")
        if not pin:
            self.log("warn", "sim", "SIM requires a PIN but none is configured in Settings")
            return "pin_required", False
        try:
            ch.send(f'AT+CPIN="{pin}"')
            self.log("info", "sim", "SIM PIN accepted")
            return "ready", True
        except ATError as e:
            self.log("error", "sim", f"SIM PIN rejected: {e}")
            return "pin_error", False

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
            auto_connect=lambda: bool(self.reload_settings_get("modem_auto_connect", 0)),
        )
        self._ppp_config_snapshot = (data_port, baud, apn, username, password)
        self.ppp.start()

    def _authorize_and_reconnect_ppp(self):
        """The one place that turns internet access ON. Shared by the
        dashboard's "Reconnect internet now" button and an inbound SMS
        saying "connect". Persists modem_auto_connect=1 (a secondary gate
        PPPSupervisor also checks - see its _supervise_loop()) and then
        explicitly authorizes + kicks the supervisor via
        request_reconnect(), which is the ONLY thing that actually opens
        its dial gate - see PPPSupervisor.request_reconnect()'s docstring.

        That authorization is deliberately NOT persisted anywhere: a fresh
        supervisor (built on every daemon start, or rebuilt here if
        settings changed) always begins unauthorized/disconnected, and a
        connection that later drops on its own also returns to
        unauthorized rather than auto-redialing forever - either way, a
        fresh call to THIS function is what's required to (re)connect.
        Never called anywhere in the normal startup/reconnect path in
        run() for exactly that reason - the daemon coming up, or the
        modem power-cycling and the control channel being re-established,
        must never by itself start dialing out."""
        db.save_settings({"modem_auto_connect": 1})
        self.reload_settings()
        if self.ppp and self._ppp_config() != self._ppp_config_snapshot:
            self.log("info", "ppp", "PPP settings changed; rebuilding supervisor before reconnecting")
            self.ppp.stop()
            self.ppp = None
        self._ensure_ppp_supervisor()
        self.ppp.request_reconnect()

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
            if not self._ping_with_retries():
                self.log("error", "at", "Control port stopped responding after retries; will attempt recovery")
                db.update_modem_status(at_ready=False, device_present=False)
                self._teardown_control_channel()
                return
            self.reload_settings()
            if self._apply_live_settings_changes():
                return  # control port changed - let the outer loop reopen it
            if time.time() - self._last_network_refresh > NETWORK_INFO_INTERVAL:
                self._refresh_network_info()

    def _ping_with_retries(self):
        """A module can briefly stop responding to AT commands during
        network registration or a RAT handover (e.g. 2G<->3G) - this is
        normal firmware behavior, not a real failure. Retry a few times
        with short gaps before concluding the control channel is actually
        dead, rather than triggering a full teardown+power-cycle over a
        single slow response."""
        for attempt in range(HEALTH_CHECK_RETRIES):
            if self.control_channel.ping(timeout=3):
                if attempt > 0:
                    self.log("info", "at",
                             f"Control port responded again after {attempt} retry(ies) - "
                             f"likely a brief busy period (e.g. network registration)")
                return True
            if attempt < HEALTH_CHECK_RETRIES - 1:
                self._sleep(HEALTH_CHECK_RETRY_GAP)
        return False

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
            new_status, just_unlocked = self._check_and_unlock_sim(self.control_channel)
            if new_status != status.get("sim_status"):
                db.update_modem_status(sim_status=new_status)
            if just_unlocked:
                self.log("info", "sim", f"Waiting {SIM_SETTLE_SECONDS}s for the SIM to settle after live PIN unlock")
                self._sleep(SIM_SETTLE_SECONDS)
                self._set_charset(self.control_channel)
                self._configure_sms(self.control_channel, retry_after_settle=True)
                try:
                    self.control_channel.send("AT^USSDMODE=0")
                except (ATError, ATTimeout):
                    pass
                try:
                    self.control_channel.send("AT+CLIP=1")
                except (ATError, ATTimeout):
                    pass

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
        if self._pending_cds_header is not None:
            # this line is the PDU-hex payload following a +CDS: <length> header
            self._handle_delivery_report(line.strip())
            self._pending_cds_header = None
            return
        if line.startswith("+CDS:"):
            self._pending_cds_header = line.strip()
            return
        if line.startswith("+CMTI:"):
            # Do NOT call ch.send() here - this callback runs on the reader
            # thread itself, and send() waits on an event that same thread
            # sets; calling it from here would deadlock. Just flag it.
            self._inbox_dirty.set()
        elif line.strip() == "RING":
            self._handle_ring()
        elif line.startswith("+CLIP:"):
            self._handle_clip(line)

    def _handle_delivery_report(self, pdu_hex):
        try:
            report = pdu.decode_status_report_pdu(pdu_hex)
        except Exception as e:
            self.log("error", "sms", f"Failed to decode delivery report: {e}")
            return
        updated = db.update_sms_ref_status(report["mr"], report["recipient"], report["status"])
        if updated:
            self.log("info", "sms",
                     f"Delivery report for {report['recipient']} (mr={report['mr']}): {report['status']}")
        else:
            self.log("warn", "sms",
                     f"Delivery report for unrecognized mr={report['mr']}/{report['recipient']}: {report['status']}")

    def _next_sms_reference(self):
        ref = self._sms_reference_counter
        self._sms_reference_counter = (self._sms_reference_counter + 1) % 256
        return ref

    # ---------------------------------------------------------------- calls
    def _handle_ring(self):
        """A bare RING with no number yet - +CLIP normally follows within
        the same ring cycle, but log something now regardless so a call
        that never gets a CLIP (withheld/unsupported) still shows up."""
        now = time.time()
        if self._current_call_id is None or (now - self._current_call_last_ring) > CALL_GAP_SECONDS:
            self._current_call_id = db.log_call_ring(number=None)
            self.log("info", "call", "Incoming call ringing (no caller ID yet)")
        else:
            db.bump_call_ring(self._current_call_id)
        self._current_call_last_ring = now
        db.update_modem_status(last_call_at=now)

    def _handle_clip(self, line):
        m = re.match(r'^\+CLIP:\s*"([^"]*)"', line.strip())
        number = (m.group(1) if m else "").strip() or "Withheld/unknown"
        now = time.time()
        if self._current_call_id is None or (now - self._current_call_last_ring) > CALL_GAP_SECONDS:
            self._current_call_id = db.log_call_ring(number=number)
        else:
            db.update_call_number(self._current_call_id, number)
        self._current_call_last_ring = now
        db.update_modem_status(last_caller=number, last_call_at=now)
        self.log("info", "call", f"Incoming call from {number}")

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
        """Copies any SIM-resident messages into the DB inbox.

        A message that's one part of a concatenated (multi-part) SMS is
        buffered instead of delivered immediately - see
        _buffer_concat_part(). Its SIM slot is deliberately NOT deleted
        yet, so it will keep showing up in AT+CMGL on every subsequent
        drain until the whole set is either complete or times out;
        _buffer_concat_part() is written to be a no-op the second+ time it
        sees the same part for exactly that reason.

        Runs on every wake of _inbox_poll_loop (URC or fallback timer)
        regardless of whether AT+CMGL actually returned anything, because
        _flush_stale_concat_sets() needs a regular heartbeat independent
        of new mail arriving.
        """
        try:
            records = sms.list_messages(ch)
        except (ATError, ATTimeout) as e:
            self.log("error", "sms", f"AT+CMGL failed: {e}")
            return
        for rec in records:
            if rec.get("concat_ref") is None:
                self._deliver_single_message(ch, rec)
            else:
                self._buffer_concat_part(ch, rec)
        self._flush_completed_concat_sets(ch)
        self._flush_stale_concat_sets(ch)

    def _deliver_single_message(self, ch, rec):
        """A normal, non-concatenated message: copy to the DB inbox and
        clear its SIM slot immediately, same as before multi-part support
        existed."""
        received_at = rec["received_at"] if rec["received_at"] is not None else time.time()
        db.add_modem_inbox_message(
            sender=rec["sender"], body=rec["body"], raw_timestamp=rec["raw_timestamp"],
            received_at=received_at, sim_index=rec["sim_index"],
        )
        self.log("info", "sms", f"New message from {rec['sender']} copied to inbox (SIM slot {rec['sim_index']})")
        self._maybe_handle_connect_sms(rec["sender"], rec["body"])
        try:
            sms.delete_message(ch, rec["sim_index"])
        except (ATError, ATTimeout) as e:
            self.log("error", "sms", f"Failed to clear SIM slot {rec['sim_index']} after copying: {e}")

    def _maybe_handle_connect_sms(self, sender, body):
        """If an inbound message's body is exactly (trimmed, case-
        insensitive) "connect", treat it the same as pressing the
        dashboard's "Reconnect internet now" button. A reassembled
        multi-part message that's missing pieces will have a bracketed
        "[incomplete message ...]" prefix, which naturally fails this
        exact-match check - it won't accidentally trigger a connect."""
        if (body or "").strip().lower() != CONNECT_SMS_KEYWORD:
            return
        self.log("info", "ppp", f"Internet connect requested via SMS from {sender}")
        self._authorize_and_reconnect_ppp()

    def _buffer_concat_part(self, ch, rec):
        seq, total = rec["concat_seq"], rec["concat_total"]
        if not (isinstance(seq, int) and isinstance(total, int) and total >= 1 and 1 <= seq <= total):
            # malformed/hostile UDH (e.g. seq > total) - don't let it sit in
            # the buffer forever waiting for a part count that makes no
            # sense; just deliver it standalone the way a non-concat
            # message would be handled.
            self.log("warn", "sms",
                     f"Message from {rec['sender']} had an invalid concat header "
                     f"(seq={seq}, total={total}) - delivering as a standalone message")
            self._deliver_single_message(ch, rec)
            return
        key = (rec["sender"], rec["concat_ref"], total)
        entry = self._concat_buffer.setdefault(
            key, {"parts": {}, "sim_indexes": {}, "first_seen": time.time()}
        )
        if seq in entry["parts"]:
            return  # already buffered this exact part on an earlier drain - not deleted yet, so it's re-listed each poll
        entry["parts"][seq] = rec
        entry["sim_indexes"][seq] = rec["sim_index"]

    def _flush_completed_concat_sets(self, ch):
        complete = [key for key in self._concat_buffer if len(self._concat_buffer[key]["parts"]) >= key[2]]
        for key in complete:
            entry = self._concat_buffer.pop(key)
            self._deliver_concat_set(ch, key, entry, timed_out=False)

    def _flush_stale_concat_sets(self, ch):
        now = time.time()
        stale = [key for key, entry in self._concat_buffer.items()
                 if now - entry["first_seen"] > CONCAT_STALE_SECONDS]
        for key in stale:
            entry = self._concat_buffer.pop(key)
            self._deliver_concat_set(ch, key, entry, timed_out=True)

    def _deliver_concat_set(self, ch, key, entry, timed_out):
        sender, concat_ref, total = key
        parts = entry["parts"]
        missing = [seq for seq in range(1, total + 1) if seq not in parts]

        body_chunks = [parts[seq]["body"] if seq in parts else f"[missing part {seq}/{total}]"
                       for seq in range(1, total + 1)]
        body = "".join(body_chunks)
        if missing:
            body = f"[incomplete message - missing part(s) {', '.join(map(str, missing))} of {total}] " + body

        anchor = parts.get(1) or min(
            parts.values(),
            key=lambda r: r["received_at"] if r["received_at"] is not None else float("inf"),
        )
        received_at = anchor["received_at"] if anchor["received_at"] is not None else time.time()

        db.add_modem_inbox_message(
            sender=sender, body=body, raw_timestamp=anchor["raw_timestamp"],
            received_at=received_at, sim_index=None,
        )

        if missing:
            self.log("warn", "sms",
                     f"Multi-part message from {sender} (ref={concat_ref}) "
                     f"{'timed out after ' + str(CONCAT_STALE_SECONDS) + 's ' if timed_out else ''}"
                     f"delivered incomplete: {len(parts)}/{total} part(s) received, "
                     f"missing {missing}")
        else:
            self.log("info", "sms",
                     f"Multi-part message from {sender} (ref={concat_ref}) reassembled "
                     f"from {total} part(s)")
        self._maybe_handle_connect_sms(sender, body)

        for seq, sim_index in entry["sim_indexes"].items():
            try:
                sms.delete_message(ch, sim_index)
            except (ATError, ATTimeout) as e:
                self.log("error", "sms",
                         f"Failed to clear SIM slot {sim_index} after reassembling multi-part message: {e}")

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
                self._authorize_and_reconnect_ppp()
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
            reference = self._next_sms_reference()
            parts = sms.send_message(ch, phone, text, reference=reference, request_status_report=True)
            for seq, total, mr in parts:
                if mr is not None:
                    db.record_sms_ref(mr, phone, part_seq=seq, part_total=total)
            refs = [mr for _, _, mr in parts]
            self.log("info", "sms", f"Sent SMS to {phone} in {len(parts)} part(s) (refs={refs})")
            db.complete_modem_command(cmd["id"], "done", json.dumps({"message_refs": refs, "parts": len(parts)}))
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
        self._ussd_waiter.reset()  # must happen before send() - see UssdWaiter.wait_for_reply()'s docstring
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
