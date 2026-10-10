// Control section of the host page: request an action, confirm it in a dialog, and read the
// command history. Every string (host names, parameters, results) is written with textContent
// only, never as markup. Changes are fetches with the session's CSRF token in X-CSRF-Token.
// Nothing here reaches a host: a request is queued and signed, and the host's daemon pulls it.
import { get, poller, seconds, whoami } from "/static/js/api.js";
import { statusChip } from "/static/js/chips.js";
import { confirmDialog, typedConfirm } from "/static/js/dialog.js";
import { toast } from "/static/js/toast.js";
import { updateResultText } from "/static/js/updates-logic.js";
import { formatWhen } from "/static/js/format.js";

const ctlBox = document.getElementById("control");
const ctlHost = (location.pathname.startsWith("/hosts/")
  ? decodeURIComponent(location.pathname.slice("/hosts/".length)) : "")
  || new URLSearchParams(location.search).get("name") || "";
const BASE = "/api/plugins/control";  // requesting and cancelling; the reads are on /api/v2/control
const ACTION_TEXT = {
  "fan.set_floor": "Set a fan floor", "fan.set_mode": "Switch fan controller mode",
  "service.restart": "Restart a service", "host.reboot": "Reboot the host",
  "agent.update": "Update the hostwatch agent",
};
const COMPONENT_TEXT = { agent: "hostwatch agent", control: "control daemon", all: "agent and control daemon" };
let ctlCsrf = "";
let ctlCaps = null;
let ctlHistoryBox = null;
let ctlNotice = null;
// Command states drawn as chips: the word is the state itself, the colour only a second signal.
const STATE_CHIP = {
  requested: "pending", pulled: "pending", scheduled: "warn", done: "up", ok: "up",
  applied: "up", failed: "down", refused: "down", cancelled: "unreachable", expired: "stale", unknown: "pending",
};

function cel(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function ctlApi(method, path, body) {
  const opts = {
    method, headers: { "X-CSRF-Token": ctlCsrf, "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  };
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  return { ok: r.ok, status: r.status, data };
}

function describe(action, params) {
  if (action === "fan.set_floor") return `${params.controller} ${params.header} floor ${params.min_duty}%`;
  if (action === "fan.set_mode") return `${params.controller} mode ${params.mode}`;
  if (action === "service.restart") return `restart ${params.name}`;
  if (action === "agent.update") return `update ${COMPONENT_TEXT[params.component] || params.component}`;
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
  } else if (action === "agent.update") {
    inputs.component = selectOf(ctlCaps.components || ["agent"]);
    for (const o of inputs.component.options) o.textContent = COMPONENT_TEXT[o.value] || o.value;
    box.append(field("Component", inputs.component));
    box.append(cel("p", "card-sub", "The host pulls the image it was installed with, replaces the agent container with the previous one kept for rollback, and reports the old and new version. The control daemon updates itself only when its allowlist permits it."));
  } else {
    box.append(cel("p", "card-sub", "The host schedules the reboot after its own delay, and you can cancel it from the history below while it waits."));
  }
  return { box, inputs };
}

function readParams(action, inputs) {
  const params = {};
  for (const [k, input] of Object.entries(inputs)) params[k] = input.value;
  if (action === "fan.set_floor") params.min_duty = Number(inputs.min_duty.value);
  return params;
}

function showError(text) {
  ctlNotice.replaceChildren(cel("h3", null, "Request refused"), cel("p", null, text));
  ctlNotice.hidden = false;
  toast(text, "down");
}

// Ask for confirmation, then queue the request. The reboot needs the host name typed exactly;
// the server checks the typed name again, so this is a convenience and not the gate.
async function submit(action, params) {
  const detail = `Host ${ctlHost}: ${describe(action, params)}. The host's own allowlist has the final say, and the request is audited.`;
  const ok = action === "host.reboot"
    ? await typedConfirm({ title: ACTION_TEXT[action], name: ctlHost, body: detail, confirmText: "Reboot" })
    : await confirmDialog({ title: ACTION_TEXT[action], body: detail, confirmText: "Confirm" });
  if (!ok) return;
  const body = { host: ctlHost, action, params, confirmed: true };
  if (action === "host.reboot") body.confirm_host = ctlHost;
  const r = await ctlApi("POST", `${BASE}/request`, body);
  if (r.ok) {
    ctlNotice.hidden = true;
    toast(`${ACTION_TEXT[action]} queued for ${ctlHost}`, "up");
    loadHistoryNow();
    return;
  }
  showError((r.data && r.data.detail && String(r.data.detail)) || `refused (${r.status})`);
}

function requestForm() {
  const form = cel("div", "ctl-form");
  const actions = ctlCaps.actions.filter((a) => a !== "host.reboot");
  const row = cel("div", "ctl-actions");
  if (actions.length) {
    const actionSel = selectOf(actions);
    for (const o of actionSel.options) o.textContent = ACTION_TEXT[o.value] || o.value;
    const holder = cel("div");
    let current = paramInputs(actionSel.value);
    holder.append(current.box);
    actionSel.addEventListener("change", () => {
      current = paramInputs(actionSel.value);
      holder.replaceChildren(current.box);
    });
    const go = cel("button", "btn primary", "Queue action");
    go.type = "button";
    go.addEventListener("click", () => {
      const action = actionSel.value;
      submit(action, readParams(action, current.inputs));
    });
    form.append(field("Action", actionSel), holder);
    row.append(go);
  }
  if (ctlCaps.actions.includes("host.reboot")) {
    const reboot = cel("button", "btn danger", "Reboot host...");
    reboot.type = "button";
    reboot.addEventListener("click", () => submit("host.reboot", {}));
    row.append(reboot);
  }
  form.append(row);
  return form;
}

// The result column: the state and the output, where an agent.update output (a JSON object
// with the old and new version) is read into "old to new".
function resultText(c) {
  const out = c.action === "agent.update" ? updateResultText(c.result.output) : c.result.output;
  return `${c.result.state}${out ? ": " + out : ""}`;
}

function historyTable(commands) {
  if (!commands.length) return cel("p", "card-sub", "No commands have been requested for this host.");
  const wrap = cel("div", "table-wrap");
  const t = cel("table", "data");
  const head = cel("tr");
  for (const h of ["Requested", "Action", "By", "State", "Result", ""]) head.append(cel("th", null, h));
  t.append(head);
  for (const c of commands) {
    const r = cel("tr");
    r.append(cel("td", null, formatWhen(seconds(c.issued_at))),
      cel("td", null, describe(c.action, c.params)), cel("td", null, c.requested_by));
    const st = cel("td");
    st.append(statusChip(STATE_CHIP[c.state] || "pending", c.state));
    r.append(st, cel("td", null, c.result ? resultText(c) : ""));
    const act = cel("td");
    if (c.state === "requested" || (c.action === "host.reboot" && c.state === "scheduled")) {
      const b = cel("button", "btn", c.state === "requested" ? "Cancel" : "Cancel reboot");
      b.type = "button";
      b.addEventListener("click", async () => {
        b.disabled = true;
        const res = await ctlApi("POST", `${BASE}/commands/${encodeURIComponent(c.id)}/cancel`);
        if (!res.ok) b.textContent = (res.data && res.data.detail) || `refused (${res.status})`;
        else loadHistoryNow();
      });
      act.append(b);
    }
    r.append(act);
    t.append(r);
  }
  wrap.append(t);
  return wrap;
}

// After a request or a cancel the history is read at once; a failed read is left to the poller.
const loadHistoryNow = () => loadHistory().catch(() => {});

async function loadHistory() {
  const page = await get("/api/v2/control/commands", { host: ctlHost, limit: 25 });
  if (ctlHistoryBox) ctlHistoryBox.replaceChildren(historyTable(page.items));
}

async function startControl() {
  if (!ctlBox || !ctlHost) return;
  try {
    const me = await whoami();
    if (!me.is_admin) return;
    ctlCsrf = me.csrf;
    const caps = await get("/api/v2/control/capabilities", { host: ctlHost });
    if (!caps.known) return;
    ctlCaps = caps;
    const h = cel("h3", null, "Control");
    h.id = "control-h";
    ctlBox.replaceChildren(h);
    ctlBox.append(cel("p", "card-sub", "Requests are signed and queued. The host pulls them and applies its own allowlist."));
    ctlNotice = cel("div", "card notice ctl-notice");
    ctlNotice.setAttribute("role", "alert");
    ctlNotice.hidden = true;
    ctlBox.append(requestForm(), ctlNotice);
    ctlBox.append(cel("h4", null, "Command history"));
    ctlHistoryBox = cel("div");
    ctlBox.append(ctlHistoryBox);
    ctlBox.hidden = false;
    poller(loadHistory, { interval: 10000 });
  } catch (_) {
    ctlBox.hidden = true;
  }
}

startControl();
