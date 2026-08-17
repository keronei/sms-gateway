"""
modem_gateway.py - sends SMS through the Huawei modem daemon
(modem/manager.py), shaped to be a drop-in alternative to gateway.py from
dispatcher.py's/app.py's point of view: same send_message()/
test_connection() call shape, same GatewayError-style exception, and a
result dict with the same "id"/"state" keys dispatcher.py already reads
off Android-gateway responses.

Sending through the modem is asynchronous under the hood - this process
(Flask/dispatcher) and the modem daemon are separate processes that only
talk through the `modem_commands` table (see db.py / modem/manager.py's
command poll loop, which picks up a new row within ~1.5s). send_message()
below enqueues a row and blocks the calling thread - dispatcher.py's
per-campaign worker thread, never the Flask request thread itself -
polling until the daemon marks it done/failed or POLL_TIMEOUT elapses.
"""
import json
import time

import db

POLL_INTERVAL = 0.5
POLL_TIMEOUT = 45  # generous: multi-segment PDU sends can take several AT round trips
STATUS_STALE_SECONDS = 30  # how old modem_status can be before we call the daemon "not running"


class GatewayError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def test_connection(settings):
    """"Reachability" for the modem path means: is the modem-manager
    daemon actually running and AT-ready right now? There's no separate
    device to dial like the Android gateway's /health check - this reads
    modem_status, which the daemon keeps fresh continuously (see the
    Modem tab), rather than sending anything."""
    status = db.get_modem_status()
    if not status or not status.get("last_updated"):
        return {"ok": False, "message": "No modem status recorded yet - is the modem-manager daemon running?"}
    age = time.time() - status["last_updated"]
    if age > STATUS_STALE_SECONDS:
        return {"ok": False, "message": f"Modem status is stale ({int(age)}s old) - the daemon may not be running."}
    if not status.get("device_present"):
        return {"ok": False, "message": "Modem is not detected. Check wiring/power - see the Modem tab."}
    if not status.get("at_ready"):
        return {"ok": False, "message": "Modem is detected but not AT-ready yet. Check the Modem tab."}
    if status.get("sim_status") not in ("ready",):
        return {"ok": False, "message": f"SIM is not ready (status: {status.get('sim_status')})."}
    return {"ok": True, "message": "Modem daemon is running and ready to send."}


def send_message(settings, phone_numbers, text):
    """Sends to the first (and, from dispatcher.py, only ever) number in
    phone_numbers - unlike the Android gateway's API, the modem dials one
    recipient per AT+CMGS, there's no batch endpoint. Returns
    {"id": <modem_command_id>, "state": "Sent", "message_refs": [...]} -
    dispatcher.py reads .get("state") the same way for both backends."""
    if not phone_numbers:
        raise GatewayError("No recipient phone number given.")
    phone = phone_numbers[0]
    if not text:
        raise GatewayError("Message text is empty.")

    cmd_id = db.enqueue_modem_command("send_sms", {"phone": phone, "text": text})

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        cmd = db.get_modem_command(cmd_id)
        if not cmd:
            raise GatewayError("Modem command vanished before completion - is the daemon running?")
        if cmd["status"] == "done":
            result = json.loads(cmd["result"] or "{}")
            return {"id": cmd_id, "state": "Sent", "message_refs": result.get("message_refs", [])}
        if cmd["status"] == "failed":
            raise GatewayError(cmd.get("result") or "Modem reported a send failure.")
        time.sleep(POLL_INTERVAL)

    raise GatewayError(
        f"Timed out after {POLL_TIMEOUT}s waiting for the modem daemon to send this message. "
        "Check the Modem tab - the daemon may be stuck, offline, or the control channel may be down."
    )


def get_message_state(settings, message_id):
    """Not meaningful for this backend - see dispatcher.py's
    refresh_statuses(), which looks up modem_sms_refs directly for the
    modem backend instead of polling a single message id, since +CDS
    delivery reports are keyed by (mr, recipient), not one opaque id per
    send. Kept only so a caller that doesn't branch on backend gets a
    clear error instead of silently doing the wrong thing."""
    raise GatewayError("get_message_state() is not used for the modem backend - see modem_sms_refs.")
