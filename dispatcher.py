"""
dispatcher.py - runs a campaign's sends in a background thread with
configurable delay/batching, and supports pause/stop from the UI.
"""
import threading
import time
import db
import gateway

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


def _worker(campaign_id, only_ids=None):
    settings = db.get_settings()
    delay = float(settings.get("delay_seconds") or 0)
    batch_size = int(settings.get("batch_size") or 0)
    batch_pause = float(settings.get("batch_pause_seconds") or 0)

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
            resp = gateway.send_message(settings, [r["phone_normalized"]], r["filled_message"])
            msg_id = resp.get("id") if isinstance(resp, dict) else None
            gw_state = resp.get("state") if isinstance(resp, dict) else None
            status = "failed" if gw_state == "Failed" else "sent"
            db.update_recipient(
                r["id"],
                status=status,
                gateway_message_id=msg_id,
                sent_at=time.time(),
                error=None if status == "sent" else "Gateway reported failure",
            )
        except gateway.GatewayError as e:
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
    """Poll the gateway for delivery state of already-sent messages.
    'sent' means submitted to the gateway; 'delivered' means the carrier/gateway
    confirmed delivery; anything still in flight (Pending/Processed/etc.) is left
    as 'sent' rather than being guessed at."""
    settings = db.get_settings()
    recipients = [
        r for r in db.get_recipients(campaign_id)
        if r["status"] in ("sent", "delivered") and r["gateway_message_id"]
    ]
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
