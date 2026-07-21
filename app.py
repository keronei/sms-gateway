import os
import io
import csv
import uuid
import json

from flask import Flask, request, jsonify, render_template, send_file, abort

import db
import merge
import gateway
import dispatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB CSV cap


def _ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _upload_path(upload_id):
    safe = "".join(c for c in upload_id if c.isalnum() or c == "-")
    return os.path.join(UPLOAD_DIR, f"{safe}.csv")


# --------------------------------------------------------------------- pages
@app.route("/")
def index():
    return render_template("dashboard.html")


# ------------------------------------------------------------------ settings
@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    s = db.get_settings()
    s = dict(s)
    s["password_set"] = bool(s.get("password"))
    s["modem_sim_pin_set"] = bool(s.get("modem_sim_pin"))
    s["modem_ppp_password_set"] = bool(s.get("modem_ppp_password"))
    s.pop("password", None)  # never echo secrets back to the browser
    s.pop("modem_sim_pin", None)
    s.pop("modem_ppp_password", None)
    return jsonify(s)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.get_json(force=True) or {}
    if data.get("password") == "":
        data.pop("password")  # blank means "leave unchanged"
    if data.get("modem_sim_pin") == "":
        data.pop("modem_sim_pin")
    if data.get("modem_ppp_password") == "":
        data.pop("modem_ppp_password")
    saved = db.save_settings(data)
    saved = dict(saved)
    saved["password_set"] = bool(saved.get("password"))
    saved["modem_sim_pin_set"] = bool(saved.get("modem_sim_pin"))
    saved["modem_ppp_password_set"] = bool(saved.get("modem_ppp_password"))
    saved.pop("password", None)
    saved.pop("modem_sim_pin", None)
    saved.pop("modem_ppp_password", None)
    return jsonify(saved)


@app.route("/api/settings/test", methods=["POST"])
def api_test_settings():
    body = request.get_json(force=True) or {}
    settings = db.get_settings()
    settings.update({k: v for k, v in body.items() if v not in (None, "")})
    result = gateway.test_connection(settings)
    return jsonify(result)


@app.route("/api/test-send", methods=["POST"])
def api_test_send():
    body = request.get_json(force=True) or {}
    phone = (body.get("phone") or "").strip()
    text = (body.get("message") or "").strip()
    if not phone or not text:
        return jsonify({"ok": False, "message": "Phone and message are required."}), 400
    settings = db.get_settings()
    normalized, valid = merge.normalize_phone(phone, settings.get("default_country_code", ""))
    if not valid:
        return jsonify({"ok": False, "message": f"'{phone}' does not look like a valid phone number."}), 400
    try:
        resp = gateway.send_message(settings, [normalized], text)
        return jsonify({"ok": True, "response": resp})
    except gateway.GatewayError as e:
        return jsonify({"ok": False, "message": str(e)}), 502


# ------------------------------------------------------------------ templates
@app.route("/api/detect-fields", methods=["POST"])
def api_detect_fields():
    body = request.get_json(force=True) or {}
    fields = merge.detect_fields(body.get("template_text", ""))
    return jsonify({"fields": fields})


@app.route("/api/templates", methods=["GET"])
def api_list_templates():
    return jsonify(db.list_templates())


@app.route("/api/templates", methods=["POST"])
def api_save_template():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    text = body.get("body") or ""
    if not name or not text.strip():
        return jsonify({"error": "Name and template body are required."}), 400
    tid = db.save_template(name, text)
    return jsonify({"id": tid})


@app.route("/api/templates/<int:template_id>", methods=["DELETE"])
def api_delete_template(template_id):
    db.delete_template(template_id)
    return jsonify({"ok": True})


@app.route("/api/sample-csv")
def api_sample_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "phone", "fee_balance", "due_date"])
    writer.writerow(["Jane Wanjiru", "0712345678", "4500", "2026-07-15"])
    writer.writerow(["John Otieno", "0798765432", "0", "2026-07-15"])
    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="sample.csv")


# ------------------------------------------------------------------- compose
@app.route("/api/csv/upload", methods=["POST"])
def api_csv_upload():
    _ensure_dirs()
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded."}), 400
    raw = file.read()
    try:
        headers, rows = merge.parse_csv(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not headers:
        return jsonify({"error": "Could not find a header row in this CSV."}), 400
    if not rows:
        return jsonify({"error": "CSV has headers but no data rows."}), 400

    upload_id = uuid.uuid4().hex
    with open(_upload_path(upload_id), "wb") as f:
        f.write(raw)

    guessed_phone = merge.guess_phone_column(headers)
    return jsonify({
        "upload_id": upload_id,
        "headers": headers,
        "row_count": len(rows),
        "sample_row": rows[0],
        "guessed_phone_column": guessed_phone,
    })


@app.route("/api/campaign/preview", methods=["POST"])
def api_campaign_preview():
    body = request.get_json(force=True) or {}
    upload_id = body.get("upload_id")
    template_text = body.get("template_text", "")
    phone_column = body.get("phone_column")
    mapping = body.get("mapping") or {}

    path = _upload_path(upload_id) if upload_id else None
    if not path or not os.path.exists(path):
        return jsonify({"error": "Upload not found or expired. Please re-upload the CSV."}), 400

    with open(path, "rb") as f:
        raw = f.read()
    headers, rows = merge.parse_csv(raw)
    settings = db.get_settings()

    fields = merge.detect_fields(template_text)
    validation = merge.validate_csv_for_template(headers, fields, phone_column)
    _, results = merge.build_preview(
        template_text, headers, rows, phone_column, mapping,
        default_country_code=settings.get("default_country_code", ""),
    )
    invalid_count = sum(1 for r in results if r["status"] == "invalid")
    return jsonify({
        "fields": fields,
        "headers": headers,
        "validation": validation,
        "results": results,
        "total": len(results),
        "invalid_count": invalid_count,
    })


@app.route("/api/campaign/create", methods=["POST"])
def api_campaign_create():
    body = request.get_json(force=True) or {}
    upload_id = body.get("upload_id")
    name = (body.get("name") or "Untitled campaign").strip()
    template_text = body.get("template_text", "")
    phone_column = body.get("phone_column")
    mapping = body.get("mapping") or {}
    skip_invalid = body.get("skip_invalid", True)

    path = _upload_path(upload_id) if upload_id else None
    if not path or not os.path.exists(path):
        return jsonify({"error": "Upload not found or expired. Please re-upload the CSV."}), 400

    with open(path, "rb") as f:
        raw = f.read()
    headers, rows = merge.parse_csv(raw)
    settings = db.get_settings()
    fields, results = merge.build_preview(
        template_text, headers, rows, phone_column, mapping,
        default_country_code=settings.get("default_country_code", ""),
    )

    if skip_invalid:
        for r in results:
            if r["status"] == "invalid":
                r["status"] = "skipped"

    campaign_id = db.create_campaign(name, template_text, fields, phone_column, mapping,
                                      os.path.basename(path))
    db.add_recipients(campaign_id, results)

    try:
        os.remove(path)
    except OSError:
        pass

    return jsonify({"campaign_id": campaign_id})


# ------------------------------------------------------------------ campaigns
@app.route("/api/campaigns", methods=["GET"])
def api_list_campaigns():
    return jsonify(db.list_campaigns())


@app.route("/api/campaigns/<int:campaign_id>", methods=["GET"])
def api_get_campaign(campaign_id):
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        abort(404)
    recipients = db.get_recipients(campaign_id)
    for r in recipients:
        r["data"] = json.loads(r.pop("data_json"))
    campaign["fields"] = json.loads(campaign.pop("fields_json"))
    campaign["mapping"] = json.loads(campaign.pop("mapping_json") or "{}")
    campaign["recipients"] = recipients
    campaign["counts"] = db.campaign_status_counts(campaign_id)
    campaign["job_state"] = dispatcher.get_job_state(campaign_id)
    return jsonify(campaign)


@app.route("/api/campaigns/<int:campaign_id>/status", methods=["GET"])
def api_campaign_status(campaign_id):
    if not db.get_campaign(campaign_id):
        abort(404)
    recipients = db.get_recipients(campaign_id)
    slim = [
        {"id": r["id"], "row_index": r["row_index"], "phone_normalized": r["phone_normalized"],
         "filled_message": r["filled_message"], "status": r["status"], "error": r["error"],
         "gateway_message_id": r["gateway_message_id"]}
        for r in recipients
    ]
    return jsonify({
        "counts": db.campaign_status_counts(campaign_id),
        "job_state": dispatcher.get_job_state(campaign_id),
        "recipients": slim,
    })


@app.route("/api/campaigns/<int:campaign_id>/dispatch", methods=["POST"])
def api_campaign_dispatch(campaign_id):
    if not db.get_campaign(campaign_id):
        abort(404)
    body = request.get_json(silent=True) or {}
    only_ids = body.get("only_ids")
    started = dispatcher.start_campaign(campaign_id, only_ids=only_ids)
    return jsonify({"started": started})


@app.route("/api/campaigns/<int:campaign_id>/pause", methods=["POST"])
def api_campaign_pause(campaign_id):
    dispatcher.pause_campaign(campaign_id)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:campaign_id>/resume", methods=["POST"])
def api_campaign_resume(campaign_id):
    dispatcher.resume_campaign(campaign_id)
    started = dispatcher.start_campaign(campaign_id) if not dispatcher.is_running(campaign_id) else True
    return jsonify({"ok": True, "started": started})


@app.route("/api/campaigns/<int:campaign_id>/stop", methods=["POST"])
def api_campaign_stop(campaign_id):
    dispatcher.stop_campaign(campaign_id)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:campaign_id>/refresh-status", methods=["POST"])
def api_campaign_refresh(campaign_id):
    updated = dispatcher.refresh_statuses(campaign_id)
    return jsonify({"updated": updated})


@app.route("/api/campaigns/<int:campaign_id>/export")
def api_campaign_export(campaign_id):
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        abort(404)
    recipients = db.get_recipients(campaign_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["row_index", "phone", "message", "status", "error", "gateway_message_id", "sent_at"])
    for r in recipients:
        writer.writerow([
            r["row_index"], r["phone_normalized"], r["filled_message"], r["status"],
            r["error"] or "", r["gateway_message_id"] or "", r["sent_at"] or "",
        ])
    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True,
                      download_name=f"campaign_{campaign_id}_results.csv")


@app.route("/api/campaigns/<int:campaign_id>", methods=["DELETE"])
def api_campaign_delete(campaign_id):
    dispatcher.stop_campaign(campaign_id)
    db.delete_campaign(campaign_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------------- modem
# Flask never touches serial ports or GPIO here - it only reads the status
# the modem-manager daemon publishes, and drops action requests into
# modem_commands for that daemon to pick up. See modem/manager.py.

@app.route("/api/modem/status")
def api_modem_status():
    status = db.get_modem_status()
    settings = db.get_settings()
    status["config"] = {
        "control_port": settings.get("modem_control_port"),
        "data_port": settings.get("modem_data_port"),
        "apn": settings.get("modem_apn"),
        "gpio_power_pin": settings.get("modem_gpio_power_pin"),
        "auto_connect": bool(settings.get("modem_auto_connect")),
        "sms_backend": settings.get("sms_backend"),
    }
    return jsonify(status)


@app.route("/api/modem/events")
def api_modem_events():
    since_id = request.args.get("since_id", type=int)
    level = request.args.get("level") or None
    category = request.args.get("category") or None
    limit = request.args.get("limit", default=200, type=int)
    events = db.get_modem_events(limit=limit, since_id=since_id, level=level, category=category)
    return jsonify(events)


@app.route("/api/modem/power-cycle", methods=["POST"])
def api_modem_power_cycle():
    cmd_id = db.enqueue_modem_command("power_cycle")
    return jsonify({"command_id": cmd_id})


@app.route("/api/modem/reconnect", methods=["POST"])
def api_modem_reconnect():
    cmd_id = db.enqueue_modem_command("reconnect_ppp")
    return jsonify({"command_id": cmd_id})


@app.route("/api/modem/commands/<int:command_id>")
def api_modem_command_status(command_id):
    cmd = db.get_modem_command(command_id)
    if not cmd:
        abort(404)
    return jsonify(cmd)


@app.route("/api/modem/inbox")
def api_modem_inbox():
    return jsonify(db.get_modem_inbox())


@app.route("/api/modem/inbox/<int:inbox_id>/read", methods=["POST"])
def api_modem_inbox_read(inbox_id):
    db.mark_modem_inbox_read(inbox_id)
    return jsonify({"ok": True})


@app.route("/api/modem/inbox/<int:inbox_id>", methods=["DELETE"])
def api_modem_inbox_delete(inbox_id):
    db.delete_modem_inbox_message(inbox_id)
    return jsonify({"ok": True})


@app.route("/api/modem/send", methods=["POST"])
def api_modem_send():
    body = request.get_json(force=True) or {}
    phone = (body.get("phone") or "").strip()
    text = (body.get("text") or "").strip()
    if not phone or not text:
        return jsonify({"error": "Phone and text are required."}), 400
    cmd_id = db.enqueue_modem_command("send_sms", {"phone": phone, "text": text})
    return jsonify({"command_id": cmd_id})


@app.route("/api/modem/calls")
def api_modem_calls():
    return jsonify(db.get_call_log())


@app.route("/api/modem/calls/<int:call_id>", methods=["DELETE"])
def api_modem_call_delete(call_id):
    db.delete_call_log_entry(call_id)
    return jsonify({"ok": True})


@app.route("/api/modem/calls", methods=["DELETE"])
def api_modem_calls_clear():
    db.clear_call_log()
    return jsonify({"ok": True})


@app.route("/api/modem/ussd/send", methods=["POST"])
def api_modem_ussd_send():
    body = request.get_json(force=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "USSD text is required."}), 400
    cmd_id = db.enqueue_modem_command("send_ussd", {"text": text})
    return jsonify({"command_id": cmd_id})


@app.route("/api/modem/ussd/end", methods=["POST"])
def api_modem_ussd_end():
    cmd_id = db.enqueue_modem_command("end_ussd_session")
    return jsonify({"command_id": cmd_id})


if __name__ == "__main__":
    _ensure_dirs()
    db.init_db()
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
