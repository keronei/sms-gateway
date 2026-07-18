// ---------------------------------------------------------------- state
const state = {
  upload: null,          // {upload_id, headers, sample_row, guessed_phone_column, row_count}
  mapping: {},           // field -> csv column
  phoneColumn: null,
  templateFields: [],
  previewResults: null,
  currentCampaignId: null,
  pollTimer: null,
  modemPollTimer: null,
  modemLogIds: new Set(),
};

const GSM7_BASIC = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";

// -------------------------------------------------------------- utilities
function $(sel) { return document.querySelector(sel); }
function $all(sel) { return Array.from(document.querySelectorAll(sel)); }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
    ...opts,
  });
  let data;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function smsSegments(text) {
  const chars = Array.from(text || "");
  const isUnicode = chars.some(ch => !GSM7_BASIC.includes(ch));
  const singleLimit = isUnicode ? 70 : 160;
  const multiLimit = isUnicode ? 67 : 153;
  const n = chars.length;
  if (n === 0) return { chars: 0, segments: 0, isUnicode };
  const segments = n <= singleLimit ? 1 : Math.ceil(n / multiLimit);
  return { chars: n, segments, isUnicode };
}

function fillLocal(template, row, mapping) {
  return (template || "").replace(/\{\s*([a-zA-Z0-9_.\-]+)\s*\}/g, (m, field) => {
    const col = mapping[field] || field;
    const v = row ? row[col] : undefined;
    return v === undefined || v === null || v === "" ? "" : String(v);
  });
}

function showMsg(el, ok, text) {
  el.textContent = text;
  el.className = "status-msg show " + (ok ? "ok" : "bad");
}

function badge(status) {
  const labels = {
    pending: "Pending", queued: "Queued", sending: "Sending", sent: "Sent",
    delivered: "Delivered", failed: "Failed", invalid: "Invalid", skipped: "Skipped",
  };
  return `<span class="badge badge-${status}">${labels[status] || status}</span>`;
}

// ------------------------------------------------------------------ tabs
$all(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});

function setTab(name) {
  $all(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $all(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "history") loadHistory();
  if (name !== "dispatch" && state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  if (name === "dispatch" && state.currentCampaignId) startPolling();
  if (name !== "modem" && state.modemPollTimer) { clearInterval(state.modemPollTimer); state.modemPollTimer = null; }
  if (name === "modem") { refreshModem(); state.modemPollTimer = setInterval(refreshModem, 3000); }
}

// -------------------------------------------------------------- settings
async function loadSettings() {
  const s = await api("/api/settings");
  $("#s_address").value = s.address || "";
  $("#s_port").value = s.port || 8080;
  $("#s_username").value = s.username || "";
  $("#s_password").placeholder = s.password_set ? "•••••••• (unchanged)" : "";
  $("#s_cc").value = s.default_country_code || "";
  $("#s_sim").value = s.sim_number || "";
  $("#s_https").checked = !!s.use_https;
  $("#s_dlr").checked = !!s.with_delivery_report;
  $("#s_delay").value = s.delay_seconds ?? 2;
  $("#s_batch").value = s.batch_size ?? 20;
  $("#s_batchpause").value = s.batch_pause_seconds ?? 15;

  $("#m_control_port").value = s.modem_control_port || "/dev/ttyUSB0";
  $("#m_data_port").value = s.modem_data_port || "/dev/ttyUSB1";
  $("#m_apn").value = s.modem_apn || "";
  $("#m_gpio_pin").value = s.modem_gpio_power_pin ?? 17;
  $("#m_ppp_user").value = s.modem_ppp_username || "";
  $("#m_ppp_pass").placeholder = s.modem_ppp_password_set ? "•••••••• (unchanged)" : "optional";
  $("#m_sim_pin").placeholder = s.modem_sim_pin_set ? "•••••••• (unchanged)" : "optional";
  $("#m_auto_connect").checked = s.modem_auto_connect === undefined ? true : !!s.modem_auto_connect;

  if (s.address) testConnection(true);
}

function gatherConnFields() {
  return {
    address: $("#s_address").value.trim(),
    port: parseInt($("#s_port").value || "8080", 10),
    username: $("#s_username").value.trim(),
    password: $("#s_password").value,
    default_country_code: $("#s_cc").value.trim(),
    sim_number: $("#s_sim").value ? parseInt($("#s_sim").value, 10) : null,
    use_https: $("#s_https").checked,
    with_delivery_report: $("#s_dlr").checked,
  };
}

$("#btnSaveSettings").addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(gatherConnFields()) });
    $("#s_password").value = "";
    showMsg($("#settingsMsg"), true, "Settings saved.");
    testConnection(true);
  } catch (e) { showMsg($("#settingsMsg"), false, e.message); }
});

$("#btnSaveThrottle").addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({
      delay_seconds: parseFloat($("#s_delay").value || "0"),
      batch_size: parseInt($("#s_batch").value || "0", 10),
      batch_pause_seconds: parseFloat($("#s_batchpause").value || "0"),
    })});
    showMsg($("#settingsMsg"), true, "Sending behaviour saved.");
  } catch (e) { showMsg($("#settingsMsg"), false, e.message); }
});

$("#btnSaveModemSettings").addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({
      modem_control_port: $("#m_control_port").value.trim(),
      modem_data_port: $("#m_data_port").value.trim(),
      modem_apn: $("#m_apn").value.trim(),
      modem_gpio_power_pin: parseInt($("#m_gpio_pin").value || "17", 10),
      modem_ppp_username: $("#m_ppp_user").value.trim(),
      modem_ppp_password: $("#m_ppp_pass").value,
      modem_sim_pin: $("#m_sim_pin").value,
      modem_auto_connect: $("#m_auto_connect").checked ? 1 : 0,
    })});
    $("#m_ppp_pass").value = "";
    $("#m_sim_pin").value = "";
    showMsg($("#modemSettingsMsg"), true, "Modem settings saved. The daemon picks these up automatically.");
  } catch (e) { showMsg($("#modemSettingsMsg"), false, e.message); }
});

async function testConnection(silent) {
  const dot = $("#connDot"), text = $("#connText");
  text.textContent = "Checking…";
  try {
    const result = await api("/api/settings/test", { method: "POST", body: JSON.stringify(gatherConnFields()) });
    dot.className = "conn-dot " + (result.ok ? "ok" : "bad");
    text.textContent = result.message;
    if (!silent) showMsg($("#settingsMsg"), result.ok, result.message);
  } catch (e) {
    dot.className = "conn-dot bad";
    text.textContent = "Check failed";
    if (!silent) showMsg($("#settingsMsg"), false, e.message);
  }
}
$("#btnTestConn").addEventListener("click", () => testConnection(false));

$("#btnTestSend").addEventListener("click", async () => {
  const phone = $("#t_phone").value.trim();
  const message = $("#t_message").value.trim();
  try {
    const r = await api("/api/test-send", { method: "POST", body: JSON.stringify({ phone, message }) });
    showMsg($("#testMsg"), r.ok, r.ok ? "Sent! Check the phone." : r.message);
  } catch (e) { showMsg($("#testMsg"), false, e.message); }
});

// --------------------------------------------------------------- compose
let detectDebounce;
$("#templateText").addEventListener("input", () => {
  clearTimeout(detectDebounce);
  detectDebounce = setTimeout(detectFields, 250);
});

async function detectFields() {
  const text = $("#templateText").value;
  const r = await api("/api/detect-fields", { method: "POST", body: JSON.stringify({ template_text: text }) });
  state.templateFields = r.fields;
  const chipsEl = $("#fieldsChips");
  chipsEl.innerHTML = r.fields.length
    ? r.fields.map(f => `<span class="chip">{${escapeHtml(f)}}</span>`).join("")
    : `<span class="chip muted">none yet</span>`;
  renderMappingRows();
  updateLivePreview();
}

$("#csvFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("/api/csv/upload", { method: "POST", body: fd });
    state.upload = r;
    state.phoneColumn = r.guessed_phone_column;
    $("#csvInfo").style.display = "block";
    const phoneSel = $("#phoneColumnSelect");
    phoneSel.innerHTML = r.headers.map(h =>
      `<option value="${escapeHtml(h)}" ${h === r.guessed_phone_column ? "selected" : ""}>${escapeHtml(h)}</option>`
    ).join("");
    phoneSel.onchange = () => { state.phoneColumn = phoneSel.value; updateLivePreview(); };
    if (!r.guessed_phone_column) {
      showMsg($("#csvValidationMsg"), false, "Couldn't guess a phone number column — please select one.");
    } else {
      showMsg($("#csvValidationMsg"), true, `Loaded ${r.row_count} row(s). Guessed phone column: "${r.guessed_phone_column}".`);
    }
    renderMappingRows();
    updateLivePreview();
  } catch (err) {
    showMsg($("#csvValidationMsg"), false, err.message);
    $("#csvInfo").style.display = "none";
  }
});

function renderMappingRows() {
  const container = $("#mappingRows");
  if (!state.upload) { container.innerHTML = `<p class="hint">Upload a CSV first.</p>`; return; }
  const headers = state.upload.headers;
  if (!state.templateFields.length) { container.innerHTML = `<p class="hint">Add {fields} to your template to map them.</p>`; return; }
  container.innerHTML = state.templateFields.map(f => {
    const auto = state.mapping[f] || (headers.includes(f) ? f : "");
    state.mapping[f] = auto;
    const options = ['<option value="">— choose column —</option>']
      .concat(headers.map(h => `<option value="${escapeHtml(h)}" ${h === auto ? "selected" : ""}>${escapeHtml(h)}</option>`));
    return `<div class="map-row">
      <div class="field-name">{${escapeHtml(f)}}</div>
      <select data-field="${escapeHtml(f)}" class="map-select">${options.join("")}</select>
    </div>`;
  }).join("");
  $all(".map-select").forEach(sel => {
    sel.addEventListener("change", () => {
      state.mapping[sel.dataset.field] = sel.value;
      updateLivePreview();
    });
  });
  const unmapped = state.templateFields.filter(f => !state.mapping[f]);
  if (unmapped.length) {
    showMsg($("#csvValidationMsg"), false, `Map every field to a CSV column — missing: ${unmapped.join(", ")}`);
  } else if (state.phoneColumn) {
    showMsg($("#csvValidationMsg"), true, "All fields mapped and phone column set. Ready to preview.");
  }
}

function updateLivePreview() {
  const template = $("#templateText").value;
  const bubble = $("#previewBubble");
  if (!template.trim()) { bubble.textContent = "Fill in a template and upload a CSV to see a preview here."; return; }
  const row = state.upload ? state.upload.sample_row : null;
  const filled = fillLocal(template, row, state.mapping);
  bubble.textContent = filled || "(empty message)";
  const { chars, segments } = smsSegments(filled);
  $("#previewChars").textContent = `${chars} chars`;
  $("#previewSegments").textContent = `${segments} segment${segments === 1 ? "" : "s"}`;
}

$("#btnGeneratePreview").addEventListener("click", async () => {
  if (!state.upload) { alert("Upload a CSV first."); return; }
  try {
    const r = await api("/api/campaign/preview", { method: "POST", body: JSON.stringify({
      upload_id: state.upload.upload_id,
      template_text: $("#templateText").value,
      phone_column: state.phoneColumn,
      mapping: state.mapping,
    })});
    state.previewResults = r;
    renderPreviewSummary(r);
    $("#btnCreateCampaign").disabled = r.total === 0;
  } catch (e) { alert(e.message); }
});

function renderPreviewSummary(r) {
  const validCount = r.total - r.invalid_count;
  $("#previewSummary").innerHTML = `
    <div class="summary-row">
      <div class="summary-tile"><div class="n">${r.total}</div><div class="l">Total rows</div></div>
      <div class="summary-tile"><div class="n" style="color:var(--green)">${validCount}</div><div class="l">Ready to send</div></div>
      <div class="summary-tile"><div class="n" style="color:var(--orange)">${r.invalid_count}</div><div class="l">Invalid (will be skipped)</div></div>
    </div>`;
  $("#previewTableCard").style.display = "block";
  $("#previewTableBody").innerHTML = r.results.slice(0, 500).map(row => `
    <tr>
      <td>${row.row_index + 1}</td>
      <td class="mono">${escapeHtml(row.phone_normalized || row.phone_raw || "—")}</td>
      <td class="msg-cell">${escapeHtml(row.filled_message)}${row.error ? `<div class="err-text">${escapeHtml(row.error)}</div>` : ""}</td>
      <td>${badge(row.status === "invalid" ? "invalid" : "pending")}</td>
    </tr>`).join("");
}

$("#btnCreateCampaign").addEventListener("click", async () => {
  if (!state.upload) return;
  const name = $("#campaignName").value.trim() || "Untitled campaign";
  try {
    const r = await api("/api/campaign/create", { method: "POST", body: JSON.stringify({
      upload_id: state.upload.upload_id,
      name,
      template_text: $("#templateText").value,
      phone_column: state.phoneColumn,
      mapping: state.mapping,
      skip_invalid: true,
    })});
    state.currentCampaignId = r.campaign_id;
    resetComposeForm();
    setTab("dispatch");
    loadCampaign(r.campaign_id);
  } catch (e) { alert(e.message); }
});

function resetComposeForm() {
  $("#templateText").value = "";
  $("#campaignName").value = "";
  $("#csvFile").value = "";
  $("#csvInfo").style.display = "none";
  $("#mappingRows").innerHTML = "";
  $("#phoneColumnSelect").innerHTML = "";
  $("#csvValidationMsg").className = "status-msg";
  $("#csvValidationMsg").textContent = "";
  $("#fieldsChips").innerHTML = '<span class="chip muted">none yet</span>';
  $("#previewSummary").innerHTML = "";
  $("#previewTableCard").style.display = "none";
  $("#previewTableBody").innerHTML = "";
  $("#previewBubble").textContent = "Fill in a template and upload a CSV to see a preview here.";
  $("#previewChars").textContent = "0 chars";
  $("#previewSegments").textContent = "0 segments";
  $("#btnCreateCampaign").disabled = true;

  state.upload = null;
  state.mapping = {};
  state.phoneColumn = null;
  state.templateFields = [];
  state.previewResults = null;
}

$("#btnResetCompose").addEventListener("click", () => {
  if (confirm("Clear the template, CSV, and preview to start a new campaign?")) resetComposeForm();
});

// template library
$("#btnSaveTemplate").addEventListener("click", async () => {
  const body = $("#templateText").value.trim();
  if (!body) { alert("Write a template first."); return; }
  const name = prompt("Name this template:");
  if (!name) return;
  await api("/api/templates", { method: "POST", body: JSON.stringify({ name, body }) });
  loadTemplateLibrary();
});

async function loadTemplateLibrary() {
  const list = await api("/api/templates");
  const sel = $("#templateLibrary");
  sel.innerHTML = '<option value="">Load saved template…</option>' +
    list.map(t => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
  sel._data = list;
}
$("#templateLibrary").addEventListener("change", (e) => {
  const list = e.target._data || [];
  const t = list.find(x => String(x.id) === e.target.value);
  if (t) { $("#templateText").value = t.body; detectFields(); }
});

// --------------------------------------------------------------- dispatch
async function loadCampaign(id) {
  const c = await api(`/api/campaigns/${id}`);
  state.currentCampaignId = id;
  $("#noCampaignCard").style.display = "none";
  $("#campaignView").style.display = "block";
  $("#dCampaignName").textContent = c.name;
  $("#dCampaignMeta").textContent = `${c.recipients.length} recipient(s) · status: ${c.status}`;
  renderCampaignBody(c.counts, c.recipients, c.job_state);
  startPolling();
}

function renderCampaignBody(counts, recipients, jobState) {
  const total = recipients.length || 1;
  const order = ["sent", "delivered", "failed", "sending", "queued", "pending", "invalid", "skipped"];
  $("#dSummary").innerHTML = order.filter(s => counts[s]).map(s =>
    `<div class="summary-tile"><div class="n">${counts[s]}</div><div class="l">${s}</div></div>`
  ).join("") || `<div class="summary-tile"><div class="n">0</div><div class="l">no recipients</div></div>`;

  const successPct = (((counts.sent || 0) + (counts.delivered || 0)) / total) * 100;
  const failPct = ((counts.failed || 0) / total) * 100;
  $("#dProgress").innerHTML = `<div class="p-sent" style="width:${successPct}%"></div><div class="p-failed" style="width:${failPct}%"></div>`;

  // jobState is only ever "run", "pause", or null/undefined (worker thread finished
  // — whether it completed, was stopped, or errored out — all look the same: idle).
  const running = jobState === "run";
  const paused = jobState === "pause";
  const idle = !running && !paused;
  const hasRemaining = (counts.pending || 0) + (counts.queued || 0) + (counts.failed || 0) > 0;

  $("#btnStart").disabled = !idle || !hasRemaining;
  $("#btnPause").disabled = !running;
  $("#btnResume").disabled = !paused;
  $("#btnStop").disabled = !(running || paused);

  $("#dispatchTableBody").innerHTML = recipients.map(r => `
    <tr>
      <td>${r.row_index + 1}</td>
      <td class="mono">${escapeHtml(r.phone_normalized || r.phone_raw || "—")}</td>
      <td class="msg-cell">${escapeHtml(r.filled_message)}${r.error ? `<div class="err-text">${escapeHtml(r.error)}</div>` : ""}</td>
      <td>${badge(r.status)}</td>
    </tr>`).join("");
}

async function refreshCampaignView() {
  if (!state.currentCampaignId) return;
  const r = await api(`/api/campaigns/${state.currentCampaignId}/status`);
  $("#dCampaignMeta").textContent = `${r.recipients.length} recipient(s)`;
  renderCampaignBody(r.counts, r.recipients, r.job_state);
}

function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refreshCampaignView, 1800);
}

$("#btnStart").addEventListener("click", async () => {
  await api(`/api/campaigns/${state.currentCampaignId}/dispatch`, { method: "POST", body: JSON.stringify({}) });
  refreshCampaignView();
});
$("#btnPause").addEventListener("click", async () => {
  await api(`/api/campaigns/${state.currentCampaignId}/pause`, { method: "POST" });
  refreshCampaignView();
});
$("#btnResume").addEventListener("click", async () => {
  await api(`/api/campaigns/${state.currentCampaignId}/resume`, { method: "POST" });
  refreshCampaignView();
});
$("#btnStop").addEventListener("click", async () => {
  if (!confirm("Stop dispatching remaining messages?")) return;
  await api(`/api/campaigns/${state.currentCampaignId}/stop`, { method: "POST" });
  refreshCampaignView();
});
$("#btnRefreshStatus").addEventListener("click", async () => {
  await api(`/api/campaigns/${state.currentCampaignId}/refresh-status`, { method: "POST" });
  refreshCampaignView();
});
$("#btnExport").addEventListener("click", () => {
  window.location = `/api/campaigns/${state.currentCampaignId}/export`;
});

// ------------------------------------------------------------------ modem
const PPP_STATE_LABEL = {
  down: "Down", dialing: "Dialing…", connected: "Connected", backoff: "Retrying…",
};
const SIM_STATUS_LABEL = {
  ready: "Ready", pin_required: "PIN required", pin_error: "PIN rejected",
  error: "Error", absent: "No SIM detected", unknown: "Unknown",
};

function csqToBars(csq) {
  if (csq === null || csq === undefined || csq === 99 || csq < 0) return "No signal reading";
  const pct = Math.round((Math.min(csq, 31) / 31) * 100);
  return `${csq}/31 (~${pct}%)`;
}

async function refreshModem() {
  try {
    const s = await api("/api/modem/status");
    renderModemStatus(s);
  } catch (e) { /* daemon's DB rows may not exist yet on a very first run; ignore */ }
  try {
    const events = await api(`/api/modem/events?limit=100`);
    renderModemLog(events);
  } catch (e) { /* ignore */ }
  try {
    const inbox = await api("/api/modem/inbox");
    renderInbox(inbox);
  } catch (e) { /* ignore */ }
}

function renderInbox(messages) {
  const tbody = $("#inboxTableBody");
  if (!messages.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state">No messages yet.</div></td></tr>`;
    return;
  }
  tbody.innerHTML = messages.map(m => `
    <tr>
      <td class="mono">${escapeHtml(m.sender || "—")}</td>
      <td class="mono" style="color:var(--muted);">${m.received_at ? new Date(m.received_at * 1000).toLocaleString() : "—"}</td>
      <td class="msg-cell">${escapeHtml(m.body)} ${m.status === "unread" ? '<span class="badge badge-queued" style="margin-left:6px;">New</span>' : ""}</td>
      <td class="actions-cell">
        <div class="row-actions">
          <button class="btn" onclick="replyToInbox(${m.id}, '${escapeHtml(m.sender || "").replace(/'/g, "\\'")}')">Reply</button>
          <button class="btn btn-danger" onclick="deleteInboxMessage(${m.id})">Delete</button>
        </div>
      </td>
    </tr>`).join("");
}

window.replyToInbox = function (id, phone) {
  api(`/api/modem/inbox/${id}/read`, { method: "POST" }).catch(() => {});
  $("#quickReplyBox").style.display = "block";
  $("#replyToPhone").textContent = phone;
  $("#replyToPhone").dataset.phone = phone;
  $("#replyText").value = "";
  $("#replyText").focus();
};

$("#btnCancelReply").addEventListener("click", () => {
  $("#quickReplyBox").style.display = "none";
});

$("#btnSendReply").addEventListener("click", async () => {
  const phone = $("#replyToPhone").dataset.phone;
  const text = $("#replyText").value.trim();
  if (!phone || !text) return;
  try {
    await api("/api/modem/send", { method: "POST", body: JSON.stringify({ phone, text }) });
    showMsg($("#inboxActionMsg"), true, `Queued for sending to ${phone}.`);
    $("#quickReplyBox").style.display = "none";
  } catch (e) { showMsg($("#inboxActionMsg"), false, e.message); }
});

window.deleteInboxMessage = async function (id) {
  if (!confirm("Delete this message?")) return;
  await api(`/api/modem/inbox/${id}`, { method: "DELETE" });
  refreshModem();
};

// ------------------------------------------------------------------- ussd
function setUssdSessionUI(active) {
  $("#ussdInputLabel").textContent = active ? "Reply" : "USSD code";
  $("#ussdInput").placeholder = active ? "Type your reply…" : "*144#";
  $("#btnUssdEnd").style.display = active ? "inline-flex" : "none";
  $("#ussdInput").style.borderColor = active ? "var(--amber)" : "";
}

$("#btnUssdSend").addEventListener("click", async () => {
  const text = $("#ussdInput").value.trim();
  if (!text) return;
  $("#btnUssdSend").disabled = true;
  showMsg($("#ussdMsg"), true, "Waiting for the network's reply (can take up to ~30s)…");
  try {
    const { command_id } = await api("/api/modem/ussd/send", { method: "POST", body: JSON.stringify({ text }) });
    const result = await pollUssdCommand(command_id);
    $("#ussdInput").value = "";
    $("#ussdResponseBox").style.display = "block";
    $("#ussdResponseText").textContent = result.text || "(no text in reply)";
    setUssdSessionUI(result.session_state === 1);
    if (result.session_state === 1) {
      showMsg($("#ussdMsg"), true, "Reply needed — the box below the network reply is now waiting for your reply.");
      $("#ussdInput").focus();
      $("#ussdInput").scrollIntoView({ behavior: "smooth", block: "center" });
    } else {
      showMsg($("#ussdMsg"), true, "Session ended.");
    }
  } catch (e) {
    showMsg($("#ussdMsg"), false, e.message);
  } finally {
    $("#btnUssdSend").disabled = false;
  }
});

$("#btnUssdEnd").addEventListener("click", async () => {
  try {
    await api("/api/modem/ussd/end", { method: "POST" });
    setUssdSessionUI(false);
    $("#ussdInput").value = "";
    showMsg($("#ussdMsg"), true, "Session ended.");
  } catch (e) { showMsg($("#ussdMsg"), false, e.message); }
});

async function pollUssdCommand(commandId, timeoutMs = 35000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const cmd = await api(`/api/modem/commands/${commandId}`);
    if (cmd.status === "done") return JSON.parse(cmd.result);
    if (cmd.status === "failed") throw new Error(cmd.result || "USSD request failed");
    await new Promise(r => setTimeout(r, 1000));
  }
  throw new Error("Timed out waiting for a reply.");
}

function renderModemStatus(s) {
  const present = !!s.device_present;
  const atReady = !!s.at_ready;
  const pppState = s.ppp_state || "down";

  $("#modemSummary").innerHTML = `
    <div class="summary-tile">
      <div class="n" style="color:${present ? 'var(--green)' : 'var(--red)'}">${present ? "Present" : "Missing"}</div>
      <div class="l">Device</div>
    </div>
    <div class="summary-tile">
      <div class="n" style="color:${atReady ? 'var(--green)' : 'var(--gray)'}">${atReady ? "Ready" : "—"}</div>
      <div class="l">AT control port</div>
    </div>
    <div class="summary-tile">
      <div class="n" style="color:${pppState === 'connected' ? 'var(--green)' : pppState === 'backoff' ? 'var(--orange)' : 'var(--gray)'}">${PPP_STATE_LABEL[pppState] || pppState}</div>
      <div class="l">Internet (PPP)</div>
    </div>
    <div class="summary-tile">
      <div class="n">${s.ppp_ip || "—"}</div>
      <div class="l">IP address</div>
    </div>`;

  const rows = [
    ["SIM status", SIM_STATUS_LABEL[s.sim_status] || s.sim_status || "—"],
    ["Signal quality", csqToBars(s.signal_quality)],
    ["Network registration", s.network_reg_status || "—"],
    ["Operator", s.operator || "—"],
    ["Control port", s.control_port || (s.config && s.config.control_port) || "—"],
    ["Data port (PPP)", (s.config && s.config.data_port) || "—"],
    ["APN", (s.config && s.config.apn) || "(none set)"],
    ["GPIO power pin", (s.config && s.config.gpio_power_pin) ?? "—"],
    ["Auto-connect", s.config && s.config.auto_connect ? "Enabled" : "Disabled"],
    ["Power cycles (this run)", s.power_cycle_count ?? 0],
    ["Retry count", s.ppp_retry_count ?? 0],
  ];
  if (pppState === "backoff" && s.ppp_next_retry_at) {
    const secs = Math.max(0, Math.round(s.ppp_next_retry_at - Date.now() / 1000));
    rows.push(["Next retry in", `${secs}s`]);
  }
  if (s.ppp_last_error) rows.push(["Last PPP error", s.ppp_last_error]);
  if (s.ussd_active) rows.push(["USSD session", "Awaiting your reply"]);

  $("#modemDetails").innerHTML = rows.map(([label, value]) => `
    <div class="field" style="margin-bottom:4px;">
      <label>${escapeHtml(label)}</label>
      <div class="mono" style="padding:4px 0;">${escapeHtml(String(value))}</div>
    </div>`).join("");

  setUssdSessionUI(!!s.ussd_active);
}

function renderModemLog(events) {
  const tbody = $("#modemLogBody");
  const nearBottom = tbody.parentElement.scrollHeight - tbody.parentElement.scrollTop - tbody.parentElement.clientHeight < 40;
  let added = false;
  for (const ev of events) {
    if (state.modemLogIds.has(ev.id)) continue;
    state.modemLogIds.add(ev.id);
    added = true;
    const tr = document.createElement("tr");
    const color = ev.level === "error" ? "var(--red)" : ev.level === "warn" ? "var(--orange)" : "var(--muted)";
    tr.innerHTML = `
      <td class="mono" style="color:var(--muted);">${new Date(ev.ts * 1000).toLocaleTimeString()}</td>
      <td><span class="chip muted" style="color:${color};">${escapeHtml(ev.category)}</span></td>
      <td class="mono">${escapeHtml(ev.message)}</td>`;
    tbody.appendChild(tr);
  }
  while (tbody.children.length > 300) tbody.removeChild(tbody.firstChild);
  if (added && nearBottom) tbody.parentElement.scrollTop = tbody.parentElement.scrollHeight;
}

$("#btnModemPowerCycle").addEventListener("click", async () => {
  if (!confirm("Pulse the modem's power pin? Only do this if it's actually unresponsive.")) return;
  try {
    await api("/api/modem/power-cycle", { method: "POST" });
    showMsg($("#modemActionMsg"), true, "Power-cycle requested — watch the log below.");
  } catch (e) { showMsg($("#modemActionMsg"), false, e.message); }
});
$("#btnModemReconnect").addEventListener("click", async () => {
  try {
    await api("/api/modem/reconnect", { method: "POST" });
    showMsg($("#modemActionMsg"), true, "Reconnect requested — watch the log below.");
  } catch (e) { showMsg($("#modemActionMsg"), false, e.message); }
});

// ---------------------------------------------------------------- history
async function loadHistory() {
  const list = await api("/api/campaigns");
  $("#historyTableBody").innerHTML = list.map(c => `
    <tr>
      <td>${escapeHtml(c.name)}</td>
      <td class="mono">${new Date(c.created_at * 1000).toLocaleString()}</td>
      <td>${c.total}</td>
      <td style="color:var(--green)">${c.sent}</td>
      <td style="color:var(--red)">${c.failed}</td>
      <td>${badge(c.status === "dispatching" ? "sending" : c.status === "completed" ? "sent" : c.status)}</td>
      <td class="actions-cell">
        <div class="row-actions">
          <button class="btn" onclick="openCampaign(${c.id})">Open</button>
          <button class="btn btn-danger" onclick="deleteCampaign(${c.id})">Delete</button>
        </div>
      </td>
    </tr>`).join("") || `<tr><td colspan="7"><div class="empty-state">No campaigns yet.</div></td></tr>`;
}

window.openCampaign = function (id) {
  setTab("dispatch");
  loadCampaign(id);
};
window.deleteCampaign = async function (id) {
  if (!confirm("Delete this campaign and all its records?")) return;
  await api(`/api/campaigns/${id}`, { method: "DELETE" });
  loadHistory();
};

// ------------------------------------------------------------------ init
loadSettings();
loadTemplateLibrary();
detectFields();
