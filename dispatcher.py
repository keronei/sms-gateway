"""
dispatcher.py - runs a campaign's sends in a background thread with
configurable delay/batching, and supports pause/stop from the UI.

Which backend actually sends (Android Gateway vs the Huawei modem) is
settings["sms_backend"], toggled from the dashboard's Settings tab -
gateway.py and modem_gateway.py present the same send_message()/
GatewayError shape so the worker loop below doesn't need to know which
one it's using beyond picking the module once per run.
"""
import json
import threading
import time
import db
import gateway
import modem_gateway

# campaign_id -> {"thread": Thread, "control": "run"|"pause"|"stop"}
_JOBS = {}
_LOCK = threading.Lock()


def _set_control(campaign_id, value):
    with _LOCK:
        if campaign_id in _JOBS:
            _JOBS[campaign_id]["control"] = value


def get_job_state(campaign_id):
    """Returns 'run' or 'pause' while a worker thread is actually alive, otherwise
    None — once the thread has finished (completed, stopped, or errored out) there
    is no active job, regardless of what control flag was last set."""
    with _LOCK:
        job = _JOBS.get(campaign_id)
        if not job:
            return None
        thread = job.get("thread")
        if not thread or not thread.is_alive():
            return None
        return job["control"] if job["control"] in ("run", "pause") else "run"


def pause_campaign(campaign_id):
    _set_control(campaign_id, "pause")
    db.set_campaign_status(campaign_id, "paused")


def resume_campaign(campaign_id):
    _set_control(campaign_id, "run")
    db.set_campaign_status(campaign_id, "dispatching")


def stop_campaign(campaign_id):
    _set_control(campaign_id, "stop")
    db.set_campaign_status(campaign_id, "stopped")


def is_running(campaign_id):
    with _LOCK:
        job = _JOBS.get(campaign_id)
        return bool(job and job["thread"].is_alive())


def _backend_module(settings):
    return modem_gateway if settings.get("sms_backend") == "modem" else gateway


def _worker(campaign_id, only_ids=None):
    settings = db.get_settings()
    delay = float(settings.get("delay_seconds") or 0)
    batch_size = int(settings.get("batch_size") or 0)
    batch_pause = float(settings.get("batch_pause_seconds") or 0)
    backend = _backend_module(settings)
    is_modem = backend is modem_gateway

    recipients = db.get_recipients(campaign_id)
    if only_ids is not None:
        only_ids = set(only_ids)
        recipients = [r for r in recipients if r["id"] in only_ids]
    # only ever (re)send messages that are actually sendable
    recipients = [r for r in recipients if r["status"] in ("pending", "queued", "failed")]

    sent_in_batch = 0
    for r in recipients:
        while True:
            state = get_job_state(campaign_id)
            if state == "stop":
                db.set_campaign_status(campaign_id, "stopped")
                return
            if state == "pause":
                time.sleep(0.5)
                continue
            break

        db.update_recipient(r["id"], status="sending")
        try:
            resp = backend.send_message(settings, [r["phone_normalized"]], r["filled_message"])
            gw_state = resp.get("state") if isinstance(resp, dict) else None
            status = "failed" if gw_state == "Failed" else "sent"
            if is_modem:
                # reusing gateway_message_id to hold the modem's TP-MR
                # value(s) (one per segment) as a JSON list, since that's
                # the correlation key refresh_statuses() needs to look up
                # delivery reports in modem_sms_refs - there's no single
                # opaque message id like the Android gateway has.
                tracking_id = json.dumps(resp.get("message_refs", [])) if isinstance(resp, dict) else None
                fail_reason = "Modem reported a send failure"
            else:
                tracking_id = resp.get("id") if isinstance(resp, dict) else None
                fail_reason = "Gateway reported failure"
            db.update_recipient(
                r["id"],
                status=status,
                gateway_message_id=tracking_id,
                sent_at=time.time(),
                error=None if status == "sent" else fail_reason,
            )
        except (gateway.GatewayError, modem_gateway.GatewayError) as e:
            db.update_recipient(r["id"], status="failed", error=str(e))

        sent_in_batch += 1
        if batch_size and sent_in_batch >= batch_size:
            sent_in_batch = 0
            if batch_pause > 0:
                for _ in range(int(batch_pause * 10)):
                    if get_job_state(campaign_id) == "stop":
                        db.set_campaign_status(campaign_id, "stopped")
                        return
                    time.sleep(0.1)
        elif delay > 0:
            for _ in range(int(delay * 10)):
                if get_job_state(campaign_id) == "stop":
                    db.set_campaign_status(campaign_id, "stopped")
                    return
                time.sleep(0.1)

    counts = db.campaign_status_counts(campaign_id)
    if get_job_state(campaign_id) != "stop":
        db.set_campaign_status(
            campaign_id,
            "completed" if not counts.get("pending") and not counts.get("queued") else "dispatching",
        )


def start_campaign(campaign_id, only_ids=None):
    if is_running(campaign_id):
        return False
    with _LOCK:
        _JOBS[campaign_id] = {"control": "run", "thread": None}
    t = threading.Thread(target=_worker, args=(campaign_id, only_ids), daemon=True)
    with _LOCK:
        _JOBS[campaign_id]["thread"] = t
    db.set_campaign_status(campaign_id, "dispatching")
    t.start()
    return True


def refresh_statuses(campaign_id):
    """Poll for delivery state of already-sent messages. 'sent' means
    submitted; 'delivered' means the carrier/gateway confirmed delivery;
    anything still in flight is left as 'sent' rather than being guessed
    at. Which backend to poll is settings["sms_backend"] at call time -
    note this can differ from whatever backend a given recipient was
    actually *sent* through if the setting was changed mid-campaign; a
    recipient sent via the other backend simply won't have a
    gateway_message_id this backend's lookup recognizes, so it's safely
    skipped rather than mismatched."""
    settings = db.get_settings()
    recipients = [
        r for r in db.get_recipients(campaign_id)
        if r["status"] in ("sent", "delivered") and r["gateway_message_id"]
    ]
    if settings.get("sms_backend") == "modem":
        return _refresh_modem_statuses(recipients)
    return _refresh_gateway_statuses(settings, recipients)


def _refresh_gateway_statuses(settings, recipients):
    updated = 0
    for r in recipients:
        try:
            resp = gateway.get_message_state(settings, r["gateway_message_id"])
        except gateway.GatewayError:
            continue
        state = resp.get("state") if isinstance(resp, dict) else None
        if state == "Delivered" and r["status"] != "delivered":
            db.update_recipient(r["id"], status="delivered", error=None)
            updated += 1
        elif state == "Failed" and r["status"] != "failed":
            db.update_recipient(r["id"], status="failed", error="Delivery failed")
            updated += 1
    return updated


def _refresh_modem_statuses(recipients):
    updated = 0
    for r in recipients:
        try:
            mrs = json.loads(r["gateway_message_id"])
        except (TypeError, ValueError):
            continue  # not a modem-tracked recipient (e.g. sent via the other backend)
        if not mrs:
            continue
        statuses = db.get_sms_ref_statuses(mrs, r["phone_normalized"], since=r["sent_at"])
        if len(statuses) < len(mrs):
            continue  # still waiting on a +CDS report for at least one segment
        if any(s == "failed" for s in statuses):
            if r["status"] != "failed":
                db.update_recipient(r["id"], status="failed", error="Delivery failed")
                updated += 1
        elif all(s == "delivered" for s in statuses):
            if r["status"] != "delivered":
                db.update_recipient(r["id"], status="delivered", error=None)
                updated += 1
    return updated
