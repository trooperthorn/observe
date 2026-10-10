// Updates page (/admin/updates, admin only): the running Observe version and commit, the
// upstream check, the Update Observe button with its typed confirmation, the host helper's
// progress as phase chips and a log panel, and the agents table with per-host and update-all
// buttons. Reads come from /api/v2 (updates/status, updates/agents, hosts, control/capabilities)
// and writes go to the admin routes with the session's CSRF token. Every string is written with
// textContent only. Nothing here reaches a host or the Docker host: the server writes a request
// file, and the control daemons pull their commands.
import { clear, el } from "/static/js/dom.js";
import { ApiError, api, get, getAll, poller, whoami } from "/static/js/api.js";
import { statusChip } from "/static/js/chips.js";
import { confirmDialog, typedConfirm } from "/static/js/dialog.js";
import { toast } from "/static/js/toast.js";
import { sortableTable } from "/static/js/table.js";
import { notAdmin, showError } from "/static/js/admin-ui.js";
import { hostHref } from "/static/js/wizard-logic.js";
import { settingsHref } from "/static/js/settings-logic.js";
import { refreshedText } from "/static/js/format.js";
import {
  agentRowState, overallChip, phaseChips, polling, pullAgeText, updateAllSummary, updateResultText,
  upstreamText, versionChanged,
} from "/static/js/updates-logic.js";

const $ = (id) => document.getElementById(id);
let csrf = "";
let loadedVersion = "";   // the version the page was served by, to detect the restart
let controlLoaded = null; // whether the control plugin answers; null until known
let agentsDraw = null;

// ---- the Observe card ------------------------------------------------------------------------

function drawStatus(status) {
  if (!loadedVersion) loadedVersion = status.version;
  $("version-line").textContent = `Running ${status.version}, commit ${status.commit}`;
  $("upstream-line").textContent = upstreamText(status.github, status.commit);
  const [state, word] = overallChip(status);
  const line = clear($("update-state"));
  line.append(statusChip(state, word));
  if (status.state && status.state.message) line.append(el("span", "muted", status.state.message));
  const chips = clear($("phases"));
  for (const [, chipState, chipWord] of phaseChips(status.state)) chips.append(statusChip(chipState, chipWord));
  const log = $("update-log");
  const lines = status.state ? status.state.log : [];
  log.hidden = lines.length === 0;
  log.textContent = lines.join("\n");
  $("update-observe").disabled = !!status.request;
  if (versionChanged(loadedVersion, status.version)) {
    $("updated-line").textContent = `Updated to ${status.version}. Reload the page to run the new console.`;
    $("updated-line").hidden = false;
  }
}

let statusPoll = null;
let fast = false;

async function refreshStatus() {
  const status = await get("/api/v2/updates/status");
  drawStatus(status);
  $("footer").textContent = refreshedText();
  const wantFast = polling(status);
  if (wantFast !== fast) {
    fast = wantFast;
    if (statusPoll) statusPoll.stop();
    statusPoll = poller(refreshStatus, { interval: fast ? 3000 : 30000, delay: fast ? 3000 : 30000,
      errorHandler: pollError });
  }
}

function pollError(err) {
  $("footer").textContent = err instanceof ApiError && err.status !== 503 ? `observe answered ${err.status}, retrying`
    : "observe unreachable (it may be restarting), retrying";
}

async function requestUpdate() {
  const ok = await typedConfirm({
    title: "Update Observe to origin/main?",
    name: "update",
    typedLabel: "Type update to confirm",
    body: "The host helper backs up the database, pulls origin/main, builds and validates the image and restarts the container. The console is unavailable for a minute, and the previous version comes back if the restart fails. The request is audited.",
    confirmText: "Update Observe",
  });
  if (!ok) return;
  try {
    const out = await api("POST", "/api/admin/updates/observe", csrf, { confirmed: true, confirm_text: "update" });
    showError($("msg"), "");
    toast(`Update requested (${out.id.slice(0, 8)}). Waiting for the host helper.`, "up");
  } catch (err) {
    showError($("msg"), err instanceof ApiError ? err.detail : "The request could not be sent.");
  }
  refreshStatus().catch(() => {});
}

// ---- the Agents card ---------------------------------------------------------------------------

async function queueFor(host) {
  const ok = await confirmDialog({
    title: "Update the hostwatch agent",
    body: `Host ${host}: update the agent container. The host's own allowlist has the final say, and the request is audited.`,
    confirmText: "Confirm",
  });
  if (!ok) return;
  try {
    await api("POST", "/api/plugins/control/request", csrf, {
      host, action: "agent.update", params: { component: "agent" }, confirmed: true,
    });
    showError($("msg"), "");
    toast(`Update queued for ${host}`, "up");
  } catch (err) {
    showError($("msg"), err instanceof ApiError ? err.detail : "The request could not be sent.");
  }
  refreshAgents().catch(() => {});
}

async function updateAll(eligible) {
  const ok = await confirmDialog({
    title: "Update every eligible agent?",
    body: `One agent.update command is queued per eligible host (${eligible} now). Each host's own allowlist and the usual rate limits apply, and hosts that cannot be queued are listed.`,
    confirmText: "Update all",
  });
  if (!ok) return;
  try {
    const out = await api("POST", "/api/plugins/control/update-agents", csrf, { component: "agent", confirmed: true });
    const summary = updateAllSummary(out);
    $("agents-summary").textContent = summary.head;
    clear($("agents-refused")).append(...summary.lines.map((t) => el("li", null, t)));
    showError($("msg"), "");
    toast(summary.head, out.queued.length ? "up" : "warn");
  } catch (err) {
    showError($("msg"), err instanceof ApiError ? err.detail : "The request could not be sent.");
  }
  refreshAgents().catch(() => {});
}

function actionCell(r) {
  const state = agentRowState(r.agent);
  if (state.kind === "update" && controlLoaded) {
    const b = el("button", "btn", "Update agent");
    b.type = "button";
    b.addEventListener("click", () => queueFor(r.host));
    return b;
  }
  const span = el("span", "muted");
  if (state.kind === "install" || state.kind === "reinstall") {
    span.append(el("span", null, state.kind === "install" ? "install command only, see " : state.text));
    const a = el("a", null, "host settings");
    a.href = settingsHref(r.host);
    span.append(a);
    return span;
  }
  span.textContent = state.text || (controlLoaded === false ? "control plugin not loaded" : "");
  return span;
}

function latestUpdate(r) {
  const c = r.last;
  if (!c) return el("span", "muted", "");
  const box = el("span");
  box.append(statusChip(c.state === "done" ? "up" : c.state === "failed" || c.state === "refused" ? "down" : "pending", c.state));
  if (c.result && c.result.output) box.append(el("span", "muted", ` ${updateResultText(c.result.output)}`));
  return box;
}

function drawAgents(rows) {
  const columns = [
    { key: "host", label: "Host", get: (r) => r.host, render: (r) => { const a = el("a", null, r.host); a.href = hostHref(r.host); return a; } },
    { key: "platform", label: "Platform", get: (r) => r.platform },
    { key: "version", label: "Agent version", get: (r) => r.agent_version || "", render: (r) => el("span", null, r.agent_version || "unknown") },
    { key: "control", label: "Control daemon", get: (r) => (r.agent && r.agent.control ? 1 : 0),
      render: (r) => (r.agent && r.agent.control ? statusChip(r.agent.control_pulled ? "up" : "pending", r.agent.control_pulled ? "Present" : "Never pulled") : statusChip("unavailable", "None")) },
    { key: "pull", label: "Last control pull", get: (r) => (r.agent && r.agent.pull_age_s !== null && r.agent.pull_age_s !== undefined ? r.agent.pull_age_s : Infinity),
      render: (r) => el("span", null, pullAgeText(r.agent ? r.agent.pull_age_s : null)) },
    { key: "last", label: "Latest update", get: (r) => (r.last ? r.last.issued_at : ""), render: latestUpdate },
    { key: "action", label: "", render: actionCell },
  ];
  const t = sortableTable({ columns, rows, empty: "No host has pushed yet.", caption: "Agents" });
  $("agents").replaceChildren(t.root);
}

async function refreshAgents() {
  const [hosts, agents] = await Promise.all([getAll("/api/v2/hosts"), get("/api/v2/updates/agents")]);
  const byHost = new Map(agents.items.map((a) => [a.host, a]));
  let latest = new Map();
  if (controlLoaded) {
    try {
      const page = await get("/api/v2/control/commands", { limit: 200 });
      for (const c of page.items) {
        if (c.action === "agent.update" && !latest.has(c.host)) latest.set(c.host, c);
      }
    } catch (_) { latest = new Map(); }
  }
  const rows = hosts.map((h) => ({
    host: h.host, platform: (byHost.get(h.host) || h).platform || h.platform, agent_version: h.agent_version,
    agent: byHost.get(h.host) || null, last: latest.get(h.host) || null,
  }));
  drawAgents(rows);
  const eligible = rows.filter((r) => r.agent && r.agent.eligible).length;
  const button = $("update-all");
  button.disabled = !controlLoaded || eligible === 0;
  button.textContent = eligible ? `Update all eligible agents (${eligible})...` : "Update all eligible agents...";
  if (agentsDraw === null) {
    agentsDraw = true;
    button.addEventListener("click", () => updateAll(rows.filter((r) => r.agent && r.agent.eligible).length));
  }
}

async function probeControl() {
  try {
    await get("/api/v2/control/capabilities", { host: "" });
    controlLoaded = true;
  } catch (err) {
    controlLoaded = !(err instanceof ApiError && err.status === 404);
  }
}

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin($("page"), "The updates page"); return; }
  csrf = me.csrf;
  $("update-observe").addEventListener("click", requestUpdate);
  await probeControl();
  if (controlLoaded === false) {
    $("agents-summary").textContent = "The control plugin is not loaded, so agents cannot be updated from here.";
  }
  statusPoll = poller(refreshStatus, { interval: 30000, errorHandler: pollError });
  poller(refreshAgents, { interval: 30000, errorHandler: () => {} });
})();
