// Control section of the host page: request an action, confirm it in a dialog, and read the
// command history. Every string (host names, parameters, results) is written with textContent
// only, never as markup. Changes are fetches with the session's CSRF token in X-CSRF-Token.
// Nothing here reaches a host: a request is queued and signed, and the host's daemon pulls it.
"use strict";

const ctlBox = document.getElementById("control");
const ctlHost = new URLSearchParams(location.search).get("name") || "";
const BASE = "/api/plugins/control";
const ACTION_TEXT = {
  "fan.set_floor": "Set a fan floor", "fan.set_mode": "Switch fan controller mode",
  "service.restart": "Restart a service", "host.reboot": "Reboot the host",
};
let ctlCsrf = "";
let ctlCaps = null;
let ctlHistoryBox = null;

function cel(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function ctlApi(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["X-CSRF-Token"] = ctlCsrf;
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  return { ok: r.ok, status: r.status, data };
}

function describe(action, params) {
  if (action === "fan.set_floor") return `${params.controller} ${params.header} floor ${params.min_duty}%`;
  if (action === "fan.set_mode") return `${params.controller} mode ${params.mode}`;
  if (action === "service.restart") return `restart ${params.name}`;
  return "reboot";
}

function field(label, input) {
  const row = cel("label", "ctl-field");
  row.append(cel("span", null, label), input);
  return row;
}

function selectOf(values) {
  const s = cel("select");
  for (const v of values) { const o = cel("option", null, v); o.value = v; s.append(o); }
  return s;
}

function paramInputs(action) {
  const inputs = {};
  const box = cel("div", "ctl-params");
  const headers = ctlCaps.capabilities.headers;
  if (action === "fan.set_floor" || action === "fan.set_mode") {
    inputs.controller = selectOf(ctlCaps.controllers);
    box.append(field("Controller", inputs.controller));
  }
  if (action === "fan.set_floor") {
    inputs.header = headers ? selectOf(headers) : cel("input");
    if (!headers) inputs.header.placeholder = "pwm2";
    inputs.min_duty = cel("input");
    inputs.min_duty.type = "number"; inputs.min_duty.min = "0"; inputs.min_duty.max = "100";
    inputs.min_duty.value = "20";
    box.append(field("Header", inputs.header), field("Minimum duty (%)", inputs.min_duty));
  } else if (action === "fan.set_mode") {
    inputs.mode = selectOf(ctlCaps.modes);
    box.append(field("Mode", inputs.mode));
  } else if (action === "service.restart") {
    inputs.name = cel("input");
    inputs.name.placeholder = "hostwatch-agent";
    box.append(field("Service name", inputs.name));
  } else {
    box.append(cel("div", "note", "The host schedules the reboot after its own delay, and you can cancel it from the history below while it waits."));
  }
  return { box, inputs };
}

function readParams(action, inputs) {
  const params = {};
  for (const [k, input] of Object.entries(inputs)) params[k] = input.value;
  if (action === "fan.set_floor") params.min_duty = Number(inputs.min_duty.value);
  return params;
}

function confirmDialog(action, params, onDone) {
  const dlg = cel("dialog", "ctl-dialog");
  dlg.append(cel("h3", null, ACTION_TEXT[action]));
  dlg.append(cel("p", null, `Host ${ctlHost}: ${describe(action, params)}.`));
  dlg.append(cel("p", "note", "The host's own allowlist has the final say, and the request is audited."));
  let typed = null;
  if (action === "host.reboot") {
    typed = cel("input");
    typed.autocomplete = "off";
    dlg.append(field(`Type the host name (${ctlHost}) to confirm a reboot`, typed));
  }
  const err = cel("div", "note ctl-error");
  const confirm = cel("button", null, "Confirm");
  const cancel = cel("button", null, "Cancel");
  cancel.type = confirm.type = "button";
  if (typed) {
    confirm.disabled = true;
    typed.addEventListener("input", () => { confirm.disabled = typed.value !== ctlHost; });
  }
  cancel.addEventListener("click", () => dlg.close());
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    const body = { host: ctlHost, action, params, confirmed: true };
    if (typed) body.confirm_host = typed.value;
    const r = await ctlApi("POST", `${BASE}/request`, body);
    if (r.ok) { dlg.close(); onDone(); return; }
    err.textContent = (r.data && r.data.detail && String(r.data.detail)) || `refused (${r.status})`;
    confirm.disabled = false;
  });
  dlg.addEventListener("close", () => dlg.remove());
  const row = cel("div", "ctl-buttons");
  row.append(cancel, confirm);
  dlg.append(err, row);
  document.body.append(dlg);
  dlg.showModal();
}

function requestForm() {
  const form = cel("div", "ctl-form");
  const actionSel = selectOf(ctlCaps.actions);
  for (const o of actionSel.options) o.textContent = ACTION_TEXT[o.value] || o.value;
  const holder = cel("div");
  let current = paramInputs(actionSel.value);
  holder.append(current.box);
  actionSel.addEventListener("change", () => {
    current = paramInputs(actionSel.value);
    holder.replaceChildren(current.box);
  });
  const go = cel("button", null, "Review request");
  go.type = "button";
  go.addEventListener("click", () => {
    const action = actionSel.value;
    confirmDialog(action, readParams(action, current.inputs), loadHistory);
  });
  form.append(field("Action", actionSel), holder, go);
  return form;
}

function historyTable(commands) {
  if (!commands.length) return cel("div", "note", "No commands have been requested for this host.");
  const t = cel("table", "items");
  const head = cel("tr");
  for (const h of ["Requested", "Action", "By", "State", "Result", ""]) head.append(cel("th", null, h));
  t.append(head);
  for (const c of commands) {
    const r = cel("tr");
    r.append(cel("td", null, new Date(c.issued_at * 1000).toLocaleString()),
      cel("td", null, describe(c.action, c.params)), cel("td", null, c.requested_by));
    const st = cel("td");
    st.append(cel("span", `pill ctl-${c.state}`, c.state));
    r.append(st, cel("td", null, c.result ? `${c.result.state}${c.result.output ? ": " + c.result.output : ""}` : ""));
    const act = cel("td");
    if (c.state === "requested" || (c.action === "host.reboot" && c.state === "scheduled")) {
      const b = cel("button", null, c.state === "requested" ? "Cancel" : "Cancel reboot");
      b.type = "button";
      b.addEventListener("click", async () => {
        b.disabled = true;
        const res = await ctlApi("POST", `${BASE}/commands/${encodeURIComponent(c.id)}/cancel`);
        if (!res.ok) b.textContent = (res.data && res.data.detail) || `refused (${res.status})`;
        else loadHistory();
      });
      act.append(b);
    }
    r.append(act);
    t.append(r);
  }
  return t;
}

async function loadHistory() {
  const r = await ctlApi("GET", `${BASE}/commands?host=${encodeURIComponent(ctlHost)}&limit=25`);
  if (r.ok && ctlHistoryBox) ctlHistoryBox.replaceChildren(historyTable(r.data.commands));
}

async function startControl() {
  if (!ctlBox || !ctlHost) return;
  try {
    const me = await ctlApi("GET", "/api/session");
    if (!me.ok || !me.data.is_admin) return;
    ctlCsrf = me.data.csrf;
    const caps = await ctlApi("GET", `${BASE}/capabilities?host=${encodeURIComponent(ctlHost)}`);
    if (!caps.ok || !caps.data.known) return;
    ctlCaps = caps.data;
    ctlBox.replaceChildren(cel("h2", null, "Control"));
    ctlBox.append(cel("div", "note", "Requests are signed and queued. The host pulls them and applies its own allowlist."));
    ctlBox.append(requestForm());
    ctlBox.append(cel("h3", null, "Command history"));
    ctlHistoryBox = cel("div");
    ctlBox.append(ctlHistoryBox);
    ctlBox.hidden = false;
    await loadHistory();
    setInterval(loadHistory, 10000);
  } catch (_) {
    ctlBox.hidden = true;
  }
}

startControl();
