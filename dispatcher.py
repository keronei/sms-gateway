"""
dispatcher.py - runs a campaign's sends in a background thread with
configurable delay/batching, and supports pause/stop from the UI.

Which backend actually sends (Android Gateway vs the Huawei modem) is
settings["sms_backend"], toggled from the dashboard's Settings tab -
gateway.py and modem_gateway.py present the same send_message()/
GatewayError shape so the worker loop below doesn't need to know which
one it's using beyond picking the module once per run.

Resilience notes (see resume_interrupted_campaigns() and the consecutive-
failure circuit breaker in _worker()):
  - _JOBS below is this PROCESS's in-memory view of which campaigns have
    a live worker thread. It does NOT survive a process restart, and it
    is NOT shared across multiple worker processes if this app is ever
    run behind a multi-worker WSGI server - see the README's "SMS
    dispatch backend" / deployment sections. This app must run as a
    SINGLE process (gunicorn -w 1, or plain `python3 app.py`) for
    dispatch state and the auto-resume-on-startup behavior below to be
    correct; more than one process would each try to resume the same
    interrupted campaign independently, sending every message twice.
  - Every recipient's status is persisted to the database as it changes,
    specifically so dispatch can always be reconstructed from the DB
    alone after a restart - _worker() never depends on anything only
    held in memory to know where it left off.
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

# Auto-pause a campaign after this many sends in a row fail. A streak this
# long is almost always a whole-channel problem (SMS bundle exhausted,
# gateway/modem unreachable) rather than that many phone numbers in a row
# happening to be bad - without this, a dead backend would just burn
# through the entire remaining recipient list marking everyone "failed"
# rather than stopping and leaving them retryable once the underlying
# issue (e.g. recharging the bundle) is fixed.
CONSECUTIVE_FAILURE_PAUSE_THRESHOLD = 5


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
    # sendable now: pending/queued/failed as before, PLUS "sending" - a
    # recipient can only be sitting at "sending" because a previous
    # worker was captured mid-send when its process stopped (crashed,
    # killed, host restarted); start_campaign()'s is_running() guard means
    # a NEW worker only ever starts once any old one for this campaign is
    # confirmed dead, so any "sending" row found here is guaranteed stale,
    # not a live in-progress send - safe (if rarely, occasionally
    # duplicate) to retry rather than leaving it stuck forever.
    recipients = [r for r in recipients if r["status"] in ("pending", "queued", "failed", "sending")]

    sent_in_batch = 0
    consecutive_failures = 0
    last_failure_reason = None
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
            if status == "sent":
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                last_failure_reason = fail_reason
        except (gateway.GatewayError, modem_gateway.GatewayError) as e:
            db.update_recipient(r["id"], status="failed", error=str(e))
            consecutive_failures += 1
            last_failure_reason = str(e)

        if consecutive_failures >= CONSECUTIVE_FAILURE_PAUSE_THRESHOLD:
            db.set_campaign_status(
                campaign_id, "paused",
                error=(
                    f"Paused automatically after {consecutive_failures} consecutive send "
                    f"failures - this usually means the backend itself is unavailable "
                    f"(SMS bundle exhausted, gateway/modem unreachable), not that this many "
                    f"phone numbers in a row are bad. Last error: {last_failure_reason}. "
                    f"Fix the underlying issue and click Resume to continue from here - "
                    f"remaining recipients were left untouched, not marked failed."
                ),
            )
            return

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
    # clear any earlier auto-pause explanation - it's now stale, the user
    # (or the startup resume sweep) has acted on it
    db.set_campaign_status(campaign_id, "dispatching", error="")
    t.start()
    return True


def resume_interrupted_campaigns():
    """Call once at process startup (see app.py). Any campaign still
    marked 'dispatching' at this point was actively being worked when the
    process last stopped - not a state a running process would otherwise
    produce - so it's safe to assume it needs a fresh worker thread, not
    that someone is deliberately leaving it in that state.

    Also resets any recipient stuck at 'sending' back to 'pending' first
    (belt-and-suspenders alongside _worker()'s own broadened recipient
    filter - see its comment - so the dashboard never shows a permanently-stuck
    'sending' recipient even for a campaign this function doesn't touch,
    e.g. one already 'completed' except for one recipient caught mid-send
    right when the process stopped).

    Returns the number of campaigns resumed."""
    reset_count = db.reset_stuck_sending_recipients()
    if reset_count:
        print(f"[INFO] dispatcher: reset {reset_count} recipient(s) stuck at 'sending' "
              f"from an interrupted run", flush=True)

    resumed = 0
    for campaign in db.get_interrupted_campaigns():
        campaign_id = campaign["id"]
        if start_campaign(campaign_id):
            resumed += 1
            print(f"[INFO] dispatcher: resumed interrupted campaign {campaign_id} "
                  f"({campaign['name']!r})", flush=True)
    return resumed


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
