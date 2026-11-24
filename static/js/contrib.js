// Contributions tab. Self-contained: relies only on the helpers app.js already
// defines ($, $all, api, escapeHtml, showMsg). Loads data only while the tab is open.
(function () {
  const cb = { regions: [], accounts: [], paybills: [], settings: null, defaults: null, offset: 0, timer: null, loaded: false };
  const PAGE = 50;
  const ksh = c => c == null ? "" : (c / 100).toLocaleString("en-KE", { minimumFractionDigits: c % 100 ? 2 : 0, maximumFractionDigits: 2 });
  const val = id => $("#" + id).value.trim();
  const checked = id => $("#" + id).checked;
  const when = ts => ts ? new Date(ts * 1000).toLocaleString() : "never";
  const regionLabel = r => (r.parent_id && cb.regions.find(x => x.id === r.parent_id) ? cb.regions.find(x => x.id === r.parent_id).name + " › " : "") + r.name;

  async function call(path, opts) {
    try { return await api("/api/contrib" + path, opts); }
    catch (e) { if (/PIN required/i.test(e.message)) showGate(); throw e; }
  }
  const post = (path, body, method = "POST") => call(path, { method, body: JSON.stringify(body || {}) });
  function flash(id, ok, text) { showMsg($("#" + id), ok, text); }
  function guard(id, fn) { return async (...a) => { try { await fn(...a); } catch (e) { if (id) flash(id, false, e.message); } }; }

  // ------------------------------------------------------------- gate
  function showGate() { $("#cbPinGate").style.display = ""; $("#cbMain").style.display = "none"; }
  async function checkGate() {
    const s = await api("/api/contrib/auth/status");
    const locked = s.pin_required && !s.authenticated;
    $("#cbPinGate").style.display = locked ? "" : "none";
    $("#cbMain").style.display = locked ? "none" : "";
    return !locked;
  }
  $("#cbPinLogin").addEventListener("click", guard("cbPinMsg", async () => {
    await post("/auth/login", { pin: $("#cbPinInput").value });
    $("#cbPinInput").value = "";
    if (await checkGate()) refreshAll();
  }));
  $("#cbLogout").addEventListener("click", guard("cbPinSetMsg", async () => { await post("/auth/logout"); await checkGate(); }));
  $("#cbSetPin").addEventListener("click", guard("cbPinSetMsg", async () => {
    const r = await post("/auth/pin", { pin: $("#cbNewPin").value });
    $("#cbNewPin").value = "";
    flash("cbPinSetMsg", true, r.pin_set ? "PIN saved." : "PIN removed.");
  }));

  // ----------------------------------------------------------- navigation
  $all("#cbNav .btn").forEach(b => b.addEventListener("click", () => openSub(b.dataset.csub)));
  function openSub(name) {
    $all("#cbNav .btn").forEach(b => b.classList.toggle("active", b.dataset.csub === name));
    $all(".cb-sub").forEach(p => p.classList.toggle("active", p.dataset.csubPanel === name));
    if (name === "overview") loadOverview(true);
    if (name === "attention") loadAttention();
    if (name === "senders") loadSenders();
    if (name === "setup") loadSetup();
    if (name === "replies") loadReplies();
    if (name === "sync") loadSync();
  }
  const currentSub = () => ($("#cbNav .btn.active") || {}).dataset.csub;

  $all(".tab-btn").forEach(b => b.addEventListener("click", () => {
    if (b.dataset.tab === "contrib") { onOpen(); }
    else if (cb.timer) { clearInterval(cb.timer); cb.timer = null; }
  }));
  async function onOpen() {
    if (!(await checkGate())) return;
    await refreshAll();
    if (!cb.timer) cb.timer = setInterval(() => { refreshState(); if (currentSub() === "overview") loadOverview(true); }, 8000);
  }
  async function refreshAll() { await loadLookups(); await refreshState(); openSub(currentSub() || "overview"); }

  // ------------------------------------------------------------- state
  async function refreshState() {
    const s = await call("/state");
    cb.settings = s.settings; cb.defaults = s.default_templates; cb.defaultPattern = s.default_pattern;
    $("#cbEnabled").checked = s.enabled;
    $("#cbEnabledLabel").textContent = s.enabled ? "ON" : "OFF";
    $(".cb-switch").classList.toggle("on", s.enabled);
    const c = s.counts;
    $("#cbChips").innerHTML = [
      `${c.contributions} payments`, `${c.senders} senders`, `${c.regions} regions`,
      `${c.queued_replies} replies waiting`,
    ].map(t => `<span class="chip muted">${t}</span>`).join("");
    $("#cbAttnCount").textContent = c.attention || "";
    const sk = $("#cbSkipped");
    if (s.enabled && c.unprocessed_inbox > 0) {
      sk.style.display = "";
      sk.innerHTML = `${c.unprocessed_inbox} older inbox message(s) arrived before the feature was switched on and were not processed. <button class="btn" id="cbBacklog">Process them</button>`;
      $("#cbBacklog").addEventListener("click", guard("cbMsg", async () => {
        const r = await post("/backlog", { from_inbox_id: 0 });
        flash("cbMsg", true, `Processing ${r.queued_messages} message(s)…`);
      }));
    } else sk.style.display = "none";
  }
  $("#cbEnabled").addEventListener("change", guard("cbMsg", async e => {
    const want = e.target.checked;
    if (!want && !confirm("Switch contributions OFF? Incoming messages will no longer be read, replied to or synced.")) { e.target.checked = true; return; }
    const r = await post("/enable", { enabled: want });
    flash("cbMsg", true, want ? "Contributions are ON." + (r.skipped_messages ? ` ${r.skipped_messages} older message(s) were left unprocessed.` : "") : "Contributions are OFF.");
    refreshState();
  }));

  async function loadLookups() {
    [cb.regions, cb.accounts, cb.paybills] = await Promise.all([call("/regions"), call("/accounts"), call("/paybills")]);
    const regOpts = (none, any) => (any ? `<option value="">${any}</option>` : "") + (none ? `<option value="none">Unassigned</option>` : "") +
      cb.regions.filter(r => r.active || any).map(r => `<option value="${r.id}">${escapeHtml(regionLabel(r))}</option>`).join("");
    const accOpts = any => (any ? `<option value="">${any}</option>` : "") + cb.accounts.map(a => `<option value="${a.id}">${escapeHtml(a.label || a.account_ref)}</option>`).join("");
    // rebuild a <select>, keeping the previous choice only if it still exists
    // (assigning a value with no matching option would blank the select)
    const keep = (sel, html) => {
      const el = $(sel), old = el.value;
      el.innerHTML = html;
      if (old && Array.from(el.options).some(o => o.value === old)) el.value = old;
    };
    keep("#cbRegion", regOpts(true, "All regions"));
    keep("#cbAccount", accOpts("All accounts"));
    keep("#cbTRegion", regOpts(false, ""));
    keep("#cbTAccount", accOpts(""));
    keep("#cbRParent", `<option value="">(none)</option>` + cb.regions.map(r => `<option value="${r.id}">${escapeHtml(regionLabel(r))}</option>`).join(""));
    keep("#cbAcPaybill", cb.paybills.map(p => `<option value="${p.id}">${p.number}${p.name ? " – " + escapeHtml(p.name) : ""}</option>`).join(""));
  }

  // ------------------------------------------------------------ overview
  const filterQS = () => {
    const p = new URLSearchParams({ period: val("cbPeriod") });
    if (val("cbPeriod") === "custom") { if (val("cbStart")) p.set("start", val("cbStart")); if (val("cbEnd")) p.set("end", val("cbEnd")); }
    if (val("cbRegion")) p.set("region_id", val("cbRegion"));
    if (val("cbAccount")) p.set("account_id", val("cbAccount"));
    return p;
  };
  $("#cbPeriod").addEventListener("change", () => $all(".cb-custom").forEach(e => e.style.display = val("cbPeriod") === "custom" ? "" : "none"));
  $("#cbApply").addEventListener("click", () => loadOverview(true));
  $("#cbExport").addEventListener("click", e => { e.currentTarget.href = "/api/contrib/contributions/export.csv?" + filterQS(); });
  $("#cbMore").addEventListener("click", () => loadOverview(false));

  async function loadOverview(reset) {
    if (reset) cb.offset = 0;
    const qs = filterQS();
    if (reset) {
      const d = await call("/summary?" + qs);
      const s = d.summary;
      $("#cbTiles").innerHTML = [
        [ksh(s.total_cents), "Collected (Ksh)"], [s.count, "Payments"], [s.contributors, "Contributors"],
        [s.last_at ? s.last_at.slice(0, 16) : "—", "Latest payment"],
      ].map(([n, l]) => `<div class="summary-tile"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
      $("#cbProgressBody").innerHTML = d.progress.map(p => {
        const pct = p.percent, bar = pct == null ? "" :
          `<div>${ksh(p.progress_cents)} of ${ksh(p.target_cents)} · ${pct}%</div><div class="cb-target-bar"><div class="${pct > 100 ? "over" : ""}" style="width:${Math.min(pct, 100)}%"></div></div>`;
        return `<tr><td>${escapeHtml(p.parent ? p.parent + " › " : "")}${escapeHtml(p.region)}</td><td>${escapeHtml(p.account)}</td>
          <td class="num">${ksh(p.collected_cents)}</td><td class="num">${p.count}</td><td class="num">${p.contributors}</td>
          <td>${bar || '<span style="color:var(--muted)">no target</span>'}</td></tr>`;
      }).join("") || `<tr><td colspan="6" class="empty-state">No payments in this period.</td></tr>`;
      $("#cbDailyBody").innerHTML = d.daily.map(r => `<tr><td>${r.day}</td><td class="num">${ksh(r.total_cents)}</td><td class="num">${r.count}</td></tr>`).join("") ||
        `<tr><td colspan="3" class="empty-state">—</td></tr>`;
      $("#cbRowsBody").innerHTML = "";
    }
    const r = await call(`/contributions?${qs}&limit=${PAGE}&offset=${cb.offset}&status=any`);
    $("#cbRowsBody").insertAdjacentHTML("beforeend", r.rows.map(x => `<tr class="${x.status === "void" ? "cb-void" : ""}">
      <td class="mono">${x.paid_at.slice(5, 16)}</td><td class="mono">${escapeHtml(x.phone)}</td><td>${escapeHtml(x.region || "Unassigned")}</td>
      <td class="num">${ksh(x.amount_cents)}</td>
      <td><button class="btn" data-void="${x.id}" data-st="${x.status}" style="padding:2px 8px;">${x.status === "void" ? "Restore" : "Void"}</button></td></tr>`).join(""));
    cb.offset += r.rows.length;
    $("#cbMore").style.display = cb.offset < r.total ? "" : "none";
  }
  $("#cbRowsBody").addEventListener("click", guard("cbMsg", async e => {
    const b = e.target.closest("[data-void]"); if (!b) return;
    const st = b.dataset.st === "void" ? "valid" : "void";
    if (st === "void" && !confirm("Void this payment? It stops counting towards totals (it can be restored).")) return;
    await post(`/contributions/${b.dataset.void}/status`, { status: st });
    loadOverview(true);
  }));

  // ------------------------------------------------------------ attention
  const regionSelect = cls => `<select class="${cls}"><option value="">Choose…</option>${cb.regions.filter(r => r.active).map(r => `<option value="${r.id}">${escapeHtml(regionLabel(r))}</option>`).join("")}</select>`;
  async function loadAttention() {
    const d = await call("/attention");
    $("#cbUnassignedBody").innerHTML = d.unassigned_senders.map(s => `<tr><td class="mono">${escapeHtml(s.phone)}</td><td class="num">${ksh(s.total_cents)}</td><td>${s.contributions}</td>
      <td><div class="cb-inline">${regionSelect("cb-assign")}<button class="btn" data-assign="${s.id}">Assign</button></div></td></tr>`).join("") ||
      `<tr><td colspan="4" class="empty-state">Everyone who paid has a region.</td></tr>`;
    $("#cbMessagesBody").innerHTML = d.messages.map(m => `<tr><td class="mono">${escapeHtml(m.phone || "—")}</td><td>${escapeHtml(m.outcome)}</td>
      <td class="msg-cell">${escapeHtml(m.detail || "")}<div style="color:var(--muted); font-size:12px;">${escapeHtml((m.body || "").slice(0, 160))}</div></td>
      <td><button class="btn" data-dismiss="${m.id}" style="padding:2px 8px;">Dismiss</button></td></tr>`).join("") ||
      `<tr><td colspan="4" class="empty-state">Nothing to review.</td></tr>`;
    $("#cbNewAccountsBody").innerHTML = d.new_accounts.map(a => `<tr><td class="mono">${escapeHtml(a.account_ref)}</td>
      <td><input type="text" class="cb-newlabel" placeholder="Project name"></td><td><button class="btn" data-label="${a.id}" data-ref="${escapeHtml(a.account_ref)}" data-pb="${a.paybill_number}">Save</button></td></tr>`).join("") ||
      `<tr><td colspan="3" class="empty-state">All account numbers are set up.</td></tr>`;
    $("#cbAttnCount").textContent = (d.messages.length + d.unassigned_senders.length + d.new_accounts.length) || "";
  }
  $("#cbUnassignedBody").addEventListener("click", guard("cbMsg", async e => {
    const b = e.target.closest("[data-assign]"); if (!b) return;
    const rid = b.parentElement.querySelector("select").value; if (!rid) return;
    const r = await post(`/senders/${b.dataset.assign}/region`, { region_id: +rid });
    flash("cbMsg", true, `Assigned. ${r.backfilled} earlier payment(s) moved to the region.`);
    loadAttention();
  }));
  $("#cbMessagesBody").addEventListener("click", guard("cbMsg", async e => {
    const b = e.target.closest("[data-dismiss]"); if (!b) return;
    await post(`/messages/${b.dataset.dismiss}/dismiss`); loadAttention(); refreshState();
  }));
  $("#cbNewAccountsBody").addEventListener("click", guard("cbMsg", async e => {
    const b = e.target.closest("[data-label]"); if (!b) return;
    const label = b.closest("tr").querySelector(".cb-newlabel").value.trim();
    const acc = cb.accounts.find(a => a.id === +b.dataset.label);
    await post(`/accounts/${b.dataset.label}`, { account_ref: acc.account_ref, paybill_id: acc.paybill_id, label, active: true }, "PUT");
    await loadLookups(); loadAttention();
  }));
  $("#cbReprocess").addEventListener("click", guard("cbMsg", async () => {
    const r = await post("/reprocess"); flash("cbMsg", true, `${r.requeued} message(s) will be read again within a few seconds.`);
  }));

  // -------------------------------------------------------------- senders
  let senderTimer = null;
  $("#cbSenderQ").addEventListener("input", () => { clearTimeout(senderTimer); senderTimer = setTimeout(loadSenders, 250); });
  async function loadSenders() {
    const rows = await call("/senders?q=" + encodeURIComponent(val("cbSenderQ")));
    $("#cbSendersBody").innerHTML = rows.map(s => `<tr><td class="mono">${escapeHtml(s.phone)}</td>
      <td><input type="text" value="${escapeHtml(s.name || "")}" data-name="${s.id}" placeholder="name" style="min-width:110px;"></td>
      <td>${escapeHtml(s.region_name || "—")}</td><td class="num">${ksh(s.total_cents)}</td>
      <td><div class="cb-inline">${regionSelect("cb-assign")}<button class="btn" data-assign="${s.id}">Set</button></div></td>
      <td><button class="btn ${s.blocked ? "btn-danger" : ""}" data-block="${s.id}" data-b="${s.blocked}" style="padding:2px 8px;">${s.blocked ? "Unblock" : "Block"}</button></td></tr>`).join("") ||
      `<tr><td colspan="6" class="empty-state">No sender found.</td></tr>`;
  }
  $("#cbSendersBody").addEventListener("click", guard("cbMsg", async e => {
    const a = e.target.closest("[data-assign]"), b = e.target.closest("[data-block]");
    if (a) {
      const rid = a.parentElement.querySelector("select").value; if (!rid) return;
      const r = await post(`/senders/${a.dataset.assign}/region`, { region_id: +rid, notify: checked("cbNotify") });
      flash("cbMsg", true, `Region set. ${r.backfilled} earlier payment(s) moved.`); loadSenders();
    }
    if (b) { await post(`/senders/${b.dataset.block}`, { blocked: b.dataset.b !== "1" }, "PUT"); loadSenders(); }
  }));
  $("#cbSendersBody").addEventListener("change", guard("cbMsg", async e => {
    if (e.target.dataset.name) await post(`/senders/${e.target.dataset.name}`, { name: e.target.value }, "PUT");
  }));

  // ---------------------------------------------------------------- setup
  async function loadSetup() {
    await loadLookups();
    $("#cbRegionsBody").innerHTML = cb.regions.map(r => `<tr class="${r.active ? "" : "cb-void"}"><td>${escapeHtml(r.name)} <span style="color:var(--muted)">${escapeHtml(r.kind)}</span></td>
      <td class="mono">${escapeHtml(r.code)}</td><td>${escapeHtml((cb.regions.find(x => x.id === r.parent_id) || {}).name || "")}</td><td>${escapeHtml(r.aliases.join(", "))}</td>
      <td><button class="btn" data-ractive="${r.id}" style="padding:2px 8px;">${r.active ? "Deactivate" : "Activate"}</button>
          <button class="btn btn-danger" data-rdel="${r.id}" style="padding:2px 8px;">Delete</button></td></tr>`).join("") ||
      `<tr><td colspan="5" class="empty-state">No regions yet.</td></tr>`;
    $("#cbAccountsBody").innerHTML = cb.accounts.map(a => `<tr class="${a.active ? "" : "cb-void"}"><td><input type="text" value="${escapeHtml(a.label || "")}" data-alabel="${a.id}" placeholder="Project name"></td>
      <td class="mono">${escapeHtml(a.account_ref)}</td><td>${a.paybill_number}</td><td>${a.active ? "yes" : "no"}</td>
      <td><button class="btn" data-aactive="${a.id}" style="padding:2px 8px;">${a.active ? "Deactivate" : "Activate"}</button>
          <button class="btn btn-danger" data-adel="${a.id}" style="padding:2px 8px;">Delete</button></td></tr>`).join("") ||
      `<tr><td colspan="5" class="empty-state">No accounts yet — they are also created automatically the first time a payment arrives.</td></tr>`;
    $("#cbPaybillChips").innerHTML = cb.paybills.map(p => `<span class="chip ${p.active ? "" : "muted"}">${p.number}${p.name ? " · " + escapeHtml(p.name) : ""}</span>`).join("");
    const t = await call("/targets");
    $("#cbTargetsBody").innerHTML = t.map(x => `<tr><td>${escapeHtml(x.region_name)}</td><td>${escapeHtml(x.account_label || x.account_ref)}</td><td class="num">${ksh(x.amount_cents)}</td>
      <td>${x.starts_on || x.ends_on ? `${x.starts_on || "…"} → ${x.ends_on || "…"}` : "open-ended"}</td>
      <td><button class="btn btn-danger" data-tdel="${x.id}" style="padding:2px 8px;">Remove</button></td></tr>`).join("") ||
      `<tr><td colspan="5" class="empty-state">No targets (optional).</td></tr>`;
  }
  $("#cbAddRegion").addEventListener("click", guard("cbRegionMsg", async () => {
    await post("/regions", { name: val("cbRName"), code: val("cbRCode"), parent_id: val("cbRParent") || null, kind: val("cbRKind") || "ward",
      aliases: val("cbRAliases").split(",").map(s => s.trim()).filter(Boolean) });
    ["cbRName", "cbRCode", "cbRAliases"].forEach(i => $("#" + i).value = "");
    flash("cbRegionMsg", true, "Region added."); loadSetup();
  }));
  $("#cbToggleBulk").addEventListener("click", () => { const b = $("#cbBulk"); b.style.display = b.style.display === "none" ? "" : "none"; });
  $("#cbBulkGo").addEventListener("click", guard("cbRegionMsg", async () => {
    const r = await post("/regions/bulk", { text: $("#cbBulkText").value, kind: val("cbRKind") || "ward" });
    flash("cbRegionMsg", !r.errors.length, `${r.created} added.` + (r.errors.length ? " Problems: " + r.errors.join("; ") : ""));
    if (r.created) { $("#cbBulkText").value = ""; loadSetup(); }
  }));
  $("#cbRegionsBody").addEventListener("click", guard("cbRegionMsg", async e => {
    const act = e.target.closest("[data-ractive]"), del = e.target.closest("[data-rdel]");
    if (act) { const r = cb.regions.find(x => x.id === +act.dataset.ractive);
      await post(`/regions/${r.id}`, { name: r.name, code: r.code, parent_id: r.parent_id, kind: r.kind, aliases: r.aliases, active: !r.active }, "PUT"); loadSetup(); }
    if (del && confirm("Delete this region?")) { await call(`/regions/${del.dataset.rdel}`, { method: "DELETE" }); loadSetup(); }
  }));
  $("#cbAddAccount").addEventListener("click", guard("cbAccountMsg", async () => {
    await post("/accounts", { account_ref: val("cbAcRef"), label: val("cbAcLabel"), paybill_id: +$("#cbAcPaybill").value, active: true });
    $("#cbAcRef").value = ""; $("#cbAcLabel").value = ""; flash("cbAccountMsg", true, "Account added."); loadSetup();
  }));
  $("#cbAddPaybill").addEventListener("click", guard("cbAccountMsg", async () => {
    await post("/paybills", { number: val("cbPbNumber"), name: val("cbPbName"), active: true });
    $("#cbPbNumber").value = ""; $("#cbPbName").value = ""; flash("cbAccountMsg", true, "Paybill added."); loadSetup();
  }));
  $("#cbAccountsBody").addEventListener("click", guard("cbAccountMsg", async e => {
    const act = e.target.closest("[data-aactive]"), del = e.target.closest("[data-adel]");
    if (act) { const a = cb.accounts.find(x => x.id === +act.dataset.aactive);
      await post(`/accounts/${a.id}`, { account_ref: a.account_ref, paybill_id: a.paybill_id, label: a.label, active: !a.active }, "PUT"); loadSetup(); }
    if (del && confirm("Delete this account?")) { await call(`/accounts/${del.dataset.adel}`, { method: "DELETE" }); loadSetup(); }
  }));
  $("#cbAccountsBody").addEventListener("change", guard("cbAccountMsg", async e => {
    if (!e.target.dataset.alabel) return;
    const a = cb.accounts.find(x => x.id === +e.target.dataset.alabel);
    await post(`/accounts/${a.id}`, { account_ref: a.account_ref, paybill_id: a.paybill_id, label: e.target.value, active: !!a.active }, "PUT");
    await loadLookups();
  }));
  $("#cbAddTarget").addEventListener("click", guard("cbTargetMsg", async () => {
    await post("/targets", { region_id: +$("#cbTRegion").value, account_id: +$("#cbTAccount").value, amount: val("cbTAmount"),
      starts_on: val("cbTStart") || null, ends_on: val("cbTEnd") || null });
    $("#cbTAmount").value = ""; flash("cbTargetMsg", true, "Target added."); loadSetup();
  }));
  $("#cbTargetsBody").addEventListener("click", guard("cbTargetMsg", async e => {
    const b = e.target.closest("[data-tdel]"); if (!b) return;
    await call(`/targets/${b.dataset.tdel}`, { method: "DELETE" }); loadSetup();
  }));

  // -------------------------------------------------------------- replies
  const TPL = [
    ["tpl_ack", "Thank-you (no target set)"], ["tpl_ack_target", "Thank-you (region has a target)"],
    ["tpl_ack_unassigned", "Thank-you + ask for ward"], ["tpl_ack_unassigned_quiet", "Thank-you (after asking enough times)"],
    ["tpl_region_set", "Ward registered"], ["tpl_region_unknown", "Ward not recognised"],
    ["tpl_duplicate", "Payment already recorded"], ["tpl_help", "Message not understood"],
  ];
  async function loadReplies() {
    const s = cb.settings;
    $("#cbRepliesOn").checked = s.replies_enabled === "1";
    $("#cbRepMax").value = s.reply_max_per_hour; $("#cbRepExpire").value = s.reply_expire_minutes; $("#cbPromptMax").value = s.region_prompt_max;
    $("#cbTemplates").innerHTML = TPL.map(([k, label]) => `<div class="field cb-tpl"><label>${label}</label>
      <textarea data-tpl="${k}">${escapeHtml(s[k] || "")}</textarea><div class="cb-prev" data-prev="${k}"></div></div>`).join("");
    $all("[data-tpl]").forEach(t => { t.addEventListener("input", () => preview(t)); preview(t); });
    $("#cbPattern").value = s.parser_pattern; $("#cbKeywords").value = s.region_keywords; $("#cbMaxAge").value = s.max_age_days; $("#cbTz").value = s.timezone;
  }
  let prevTimers = {};
  function preview(t) {
    clearTimeout(prevTimers[t.dataset.tpl]);
    prevTimers[t.dataset.tpl] = setTimeout(async () => {
      try {
        const r = await post("/templates/preview", { template: t.value });
        $(`[data-prev="${t.dataset.tpl}"]`).innerHTML = r.text ? `Example: <b>${escapeHtml(r.text)}</b> · ${r.chars} chars, ${r.segments} SMS` : "Empty — nothing will be sent for this case.";
      } catch (e) { /* ignore */ }
    }, 250);
  }
  $("#cbSaveReplies").addEventListener("click", guard("cbRepliesMsg", async () => {
    const body = { replies_enabled: checked("cbRepliesOn"), reply_max_per_hour: val("cbRepMax"), reply_expire_minutes: val("cbRepExpire"), region_prompt_max: val("cbPromptMax") };
    $all("[data-tpl]").forEach(t => body[t.dataset.tpl] = t.value);
    await post("/settings", body); flash("cbRepliesMsg", true, "Saved."); refreshState();
  }));
  $("#cbTestParse").addEventListener("click", guard("cbParseMsg", async () => {
    const r = await post("/parser/test", { body: $("#cbTestBody").value, pattern: $("#cbPattern").value });
    const out = $("#cbParseOut"); out.style.display = "";
    out.textContent = r.matches.length ? r.matches.map(m => JSON.stringify(m, null, 1)).join("\n") : "No payment found in that message.";
    flash("cbParseMsg", r.matches.length > 0, r.matches.length ? `${r.matches.length} payment(s) found.` : "Pattern did not match.");
  }));
  $("#cbSavePattern").addEventListener("click", guard("cbParseMsg", async () => {
    await post("/settings", { parser_pattern: $("#cbPattern").value, region_keywords: val("cbKeywords"), max_age_days: val("cbMaxAge"), timezone: val("cbTz") });
    flash("cbParseMsg", true, "Saved."); refreshState();
  }));
  $("#cbResetPattern").addEventListener("click", () => { $("#cbPattern").value = cb.defaultPattern; });

  // ----------------------------------------------------------------- sync
  async function loadSync() {
    const [st] = await Promise.all([call("/sync/status"), refreshState()]);
    const s = cb.settings;
    $("#cbSheetsOn").checked = s.sheets_enabled === "1"; $("#cbSheetId").value = s.sheets_spreadsheet_id; $("#cbSheetEvery").value = s.sheets_interval_minutes;
    $("#cbTabC").value = s.sheets_contributions_tab; $("#cbTabS").value = s.sheets_summary_tab;
    $("#cbUseModem").checked = s.sheets_use_modem === "1"; $("#cbMask").checked = s.sheets_mask_phones === "1";
    $("#cbSaHint").textContent = s.sheets_service_account_set ? `Key stored for ${s.sheets_client_email}. Share the spreadsheet with that address as an Editor.` : "Paste the JSON key downloaded from Google Cloud.";
    const chips = [st.pending ? "changes waiting" : "up to date", `last success: ${when(st.last_success_at)}`];
    if (st.running) chips.unshift("syncing…");
    if (st.last_error) chips.push("last error: " + st.last_error);
    $("#cbSyncStatus").innerHTML = chips.map((c, i) => `<span class="chip ${/error/.test(c) ? "" : "muted"}">${escapeHtml(c)}</span>`).join("");
  }
  $("#cbSaveSheets").addEventListener("click", guard("cbSheetsMsg", async () => {
    await post("/settings", { sheets_enabled: checked("cbSheetsOn"), sheets_spreadsheet_id: val("cbSheetId"), sheets_interval_minutes: val("cbSheetEvery"),
      sheets_contributions_tab: val("cbTabC"), sheets_summary_tab: val("cbTabS"), sheets_use_modem: checked("cbUseModem"), sheets_mask_phones: checked("cbMask"),
      sheets_service_account_json: $("#cbSaJson").value });
    $("#cbSaJson").value = ""; flash("cbSheetsMsg", true, "Saved."); loadSync();
  }));
  async function slow(btn, id, path, label) {
    btn.disabled = true; flash(id, true, label);
    try { const r = await post(path); flash(id, r.ok, r.message || r.error); }
    catch (e) { flash(id, false, e.message); }
    btn.disabled = false; loadSync();
  }
  $("#cbTestSheets").addEventListener("click", e => slow(e.currentTarget, "cbSheetsMsg", "/sync/test", "Connecting… this can take up to two minutes if the modem has to get online."));
  $("#cbRunSync").addEventListener("click", e => slow(e.currentTarget, "cbSheetsMsg", "/sync/run", "Syncing… this can take up to two minutes if the modem has to get online."));

  // initial: if the tab is already the active one on load
  if ($("#tab-contrib").classList.contains("active")) onOpen();
})();
