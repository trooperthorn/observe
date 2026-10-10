// Host settings page (/hosts/<name>/settings, admin only): the control allowlist with a diff
// confirm, the short update command, the full install command, cleanup of a machine, and the
// danger zone. The server makes every command and token; this page only shows them. A command
// lives in memory only: it never goes into the URL, storage or a toast. Everything from the
// server is written with textContent.
import { el, clear } from "/static/js/dom.js";
import { api, poller, whoami } from "/static/js/api.js";
import { statusChip } from "/static/js/chips.js";
import { confirmDialog, typedConfirm } from "/static/js/dialog.js";
import { toast } from "/static/js/toast.js";
import { notAdmin, showError, copyText } from "/static/js/admin-ui.js";
import { NEEDS_URL, askPublicUrl } from "/static/js/public-url.js";
import { activeKeysText } from "/static/js/keys-logic.js";
import { formatWhen } from "/static/js/format.js";
import {
  validHeader, validService, progressChip, reportChip, noticeFor, guardText,
} from "/static/js/wizard-logic.js";
import {
  hostFromPath, draftFromAllowlist, allowlistFromDraft, diffAllowlist, allowlistChip,
  allowlistHelp, taskChip, taskTitle, shouldPoll, agentText, dataPathText,
} from "/static/js/settings-logic.js";

const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);
const msg = $("msg");
const host = hostFromPath(window.location.pathname);
const when = (ts) => (ts ? formatWhen(ts) : "");
const hostUrl = (tail) => `/api/hosts/${encodeURIComponent(host)}${tail}`;       // changes
const hostRead = (tail) => `/api/v2/hosts/${encodeURIComponent(host)}${tail}`;  // reads

let csrf = "";
let settings = null;     // the last GET /api/v2/hosts/{host}/settings
let draft = null;        // the editable allowlist
let shown = null;        // { kind: "update" | "cleanup" | "install", made } the command on screen
let install = null;      // the last enrolment progress while an install command is being watched
let pollHandle = null;

// The confirm-the-address box: the install command never uses the address this page was reached at.
const urlParts = { box: $("public-url-box"), form: $("public-url-form"), input: $("public-url"), status: $("public-url-status") };

// ---- identity ----
function drawIdentity() {
  const s = settings;
  $("title").textContent = `${host} settings`;
  document.title = `${host} settings - Observe`;
  const crumbs = clear($("crumbs"));
  const home = el("a", null, "Hosts");
  home.href = "/hosts";
  const page = el("a", null, host);
  page.href = `/hosts/${encodeURIComponent(host)}`;
  crumbs.append(home, " / ", page, " / Settings");
  const rows = [["Host", host]];
  if (s.enrolled) {
    rows.push(["Platform", s.platform_label]);
    rows.push(["Agent", agentText(s)]);
    rows.push(["Control", s.control ? "thermal-control chosen" : "not chosen"]);
    rows.push(["Added", `${formatWhen(s.created)}${s.created_by ? ` by ${s.created_by}` : ""}`]);
    rows.push(["Install command", s.installed ? "has been run" : "not run yet"]);
  } else {
    rows.push(["Agent", agentText(s)]);
  }
  const data = dataPathText(s);
  if (data) rows.push(["Data", data]);
  rows.push(["Active keys", s.active_by_scope ? activeKeysText(s.active_by_scope) : String(s.active_keys)]);
  const list = clear($("identity-list"));
  for (const [k, v] of rows) list.append(el("dt", null, k), el("dd", null, v));
  $("identity-note").textContent = s.enrolled ? ""
    : data ? "Observe polls this host itself, so it has no keys to revoke; its data stops when the monitor above is removed or disabled in the YAML."
    : "This host was not added through the console, so its allowlist and install command are not managed here. You can still revoke its keys or remove it below.";
  $("reporting").replaceChildren(statusChip(s.reporting ? "up" : "pending", s.reporting ? "Reporting" : "Not reporting"));
}

// ---- allowlist ----
function listItem(item, withLimit) {
  const li = el("li");
  const label = el("label", "inline");
  const box = el("input");
  box.type = "checkbox";
  box.checked = item.on;
  box.addEventListener("change", () => { item.on = box.checked; });
  label.append(box, el("span", null, item.name));
  li.append(label);
  if (withLimit) {
    const l = el("label", "wiz-limit");
    const input = el("input");
    input.type = "text";
    input.inputMode = "numeric";
    input.value = item.limit;
    input.maxLength = 3;
    input.setAttribute("aria-label", `Lowest duty percent a remote request may set for ${item.name}`);
    input.addEventListener("input", () => { item.limit = input.value; });
    l.append(el("span", null, "Lowest remote duty %"), input);
    li.append(l);
  }
  return li;
}

function drawAllowlist() {
  const s = settings;
  const show = s.enrolled && s.control;
  $("allowlist-card").hidden = !show;
  if (!show) return;
  clear($("fan-list")).append(...draft.fans.map((f) => listItem(f, true)));
  clear($("service-list")).append(...draft.services.map((x) => listItem(x, false)));
  $("reboot").checked = draft.reboot;
  $("update-agent").checked = draft.update;
  drawAllowlistState();
}

// The saved allowlist's status chip and the next step, redrawn on every poll without touching
// the form the person is editing.
function drawAllowlistState() {
  const s = settings;
  const st = s.allowlist_status || { state: "none" };
  const [state, word] = allowlistChip(st.state);
  const line = clear($("allowlist-state"));
  line.append(el("span", null, "Status of the saved allowlist:"), statusChip(state, word));
  if (st.applied_at) line.append(el("span", "muted", when(st.applied_at)));
  const help = allowlistHelp(st, s.installed);
  if (help) line.append(el("span", "muted", help));
  $("update-again").hidden = !(s.can_update && s.installed && (st.state === "pending" || st.state === "written"));
}

function addEntry(inputId, list, ok, what) {
  const input = $(inputId);
  const name = input.value.trim();
  if (!name) return;
  if (!ok(name)) { $("allow-error").textContent = `That ${what} has characters that are not allowed.`; return; }
  $("allow-error").textContent = "";
  const found = list.find((x) => x.name === name);
  if (found) found.on = true; else list.push({ name, on: true, limit: "" });
  input.value = "";
  drawAllowlist();
}

$("fan-add").addEventListener("click", () => addEntry("fan-new", draft.fans, validHeader, "header"));
$("service-add").addEventListener("click", () => addEntry("service-new", draft.services, validService, "service name"));
$("reboot").addEventListener("change", () => { draft.reboot = $("reboot").checked; });
$("update-agent").addEventListener("change", () => { draft.update = $("update-agent").checked; });

$("form-allow").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("allow-error").textContent = "";
  const built = allowlistFromDraft(draft);
  if (built.error) { $("allow-error").textContent = built.error; return; }
  const lines = diffAllowlist(settings.allowlist, built.allowlist);
  if (!lines.length) { $("allow-error").textContent = "Nothing has changed."; return; }
  const note = settings.installed
    ? "Saving makes an update command. Run it on the host to rewrite control.toml there. The host keeps its earlier allowlist until then."
    : "The install command has not been run yet, so the new list goes into it.";
  const ok = await confirmDialog({ title: `Save the allowlist of ${host}?`, body: note, lines, confirmText: "Save allowlist" });
  if (!ok) return;
  $("save").disabled = true;
  try {
    const out = await api("PUT", hostUrl("/allowlist"), csrf, { ...built.allowlist, confirmed: true });
    toast("Allowlist saved.", "up");
    if (out.command) {
      shown = { kind: "update", made: out };
      install = null;
    } else if (out.command_error) {
      showError(msg, `Saved, but no update command was made: ${out.command_error}`);
    }
    await reload(true);
  } catch (err) {
    if (err.message !== "not signed in") showError(msg, err.message);
  } finally {
    $("save").disabled = false;
  }
});

$("update-again").addEventListener("click", async () => {
  try {
    const made = await api("POST", hostUrl("/tasks"), csrf, { kind: "update", confirmed: true });
    shown = { kind: "update", made };
    install = null;
    await reload(false);
  } catch (err) {
    if (err.message !== "not signed in") showError(msg, err.message);
  }
});

// ---- command and progress ----
function drawCommand() {
  const card = $("command-card");
  const task = settings && settings.task;
  const live = shown && shown.made && shown.made.command;
  // A task with no command on screen (the page was reloaded) is still worth showing: its
  // progress, and a note that the command cannot be shown again.
  const orphan = !shown && task && (task.state === "waiting" || task.state === "fetched" || task.state === "failed");
  card.hidden = !(shown || orphan);
  if (card.hidden) return;
  const kind = shown ? shown.kind : task.kind;
  $("command-h").textContent = kind === "install" ? `Install command for ${host}` : `${taskTitle(kind)}: ${host}`;
  const block = $("command-block");
  block.hidden = !live;
  if (live) {
    $("cmd").textContent = shown.made.command;
    $("command-sub").textContent = `${shown.made.platform_label}. Run it on ${host} only.`;
    $("expiry").textContent = `Expires at ${when(shown.made.expires_at)} (${Math.round(shown.made.ttl_s / 60)} minutes) and works once.`;
  } else {
    $("command-sub").textContent = "";
    $("expiry").textContent = orphan && task.state === "waiting"
      ? "A command was made but it cannot be shown again, because only a digest is kept. Make a new one."
      : "";
  }
  $("command-note").textContent = kind === "cleanup"
    ? "Run this only on the machine that holds the install made for this host. It refuses any other machine and changes nothing there."
    : kind === "update"
      ? "The script refuses to run on any other machine than this host, and on the Observe host."
      : "The script refuses to run on any other machine than this host, and on the Observe host. The keys of the old install are revoked.";
  drawProgress(kind, task);
}

function drawProgress(kind, task) {
  const list = clear($("progress"));
  const reports = clear($("reports"));
  if (kind === "install") {
    if (!install) return;
    for (const s of install.steps) {
      const [chipState, word] = progressChip(s.status);
      const li = el("li", s.status);
      li.append(el("span", "p-label", s.label), statusChip(chipState, word));
      if (s.at) li.append(el("span", "p-time", when(s.at)));
      list.append(li);
    }
    drawReports(reports, install.install || []);
    return;
  }
  if (!task) return;
  const [chipState, word] = taskChip(task.state);
  const li = el("li", task.state);
  li.append(el("span", "p-label", taskTitle(task.kind)), statusChip(chipState, word));
  if (task.fetched_at) li.append(el("span", "p-time", when(task.fetched_at)));
  list.append(li);
  drawReports(reports, task.install || []);
}

function drawReports(reports, items) {
  for (const r of items) {
    const [chipState, word] = reportChip(r.status);
    const li = el("li");
    li.append(el("span", "p-label", `Step: ${r.step}`), statusChip(chipState, word));
    if (r.at) li.append(el("span", "p-time", when(r.at)));
    if (r.note) li.append(el("span", "p-note", r.note));
    reports.append(li);
  }
}

$("copy").addEventListener("click", () => { if (shown && shown.made) copyText(shown.made.command, $("cmd")); });

// ---- install command, cleanup ----
function drawInstallCard() {
  const s = settings;
  $("install-card").hidden = !s.enrolled;
  drawTokenState();
  $("cleanup").hidden = !s.can_cleanup || !s.installed;
  $("pool-field").hidden = s.platform !== "truenas";
}

// The install command's own state: waiting, already used or expired (each with Regenerate below),
// and why the script refused to run on a machine, which leaves the command valid.
function drawTokenState() {
  const e = settings.enrolment;
  const line = $("token-state");
  const guard = $("guard-note");
  if (!e) { line.textContent = ""; guard.hidden = true; return; }
  const notice = noticeFor({ ready: settings.reporting && e.token_state === "used", expired: e.token_state === "expired", token_state: e.token_state, stalled: e.stalled });
  if (notice) line.textContent = `${notice.title}. ${notice.text}`;
  else if (e.token_state === "valid") line.textContent = `The install command has not been run yet. It works once and expires at ${when(e.expires_at)}.`;
  else line.textContent = "";
  const refused = guardText({ guard: e.guard });
  guard.hidden = !refused;
  guard.textContent = refused ? `${refused} The command is still valid; run it on ${host}.` : "";
}

$("regen").addEventListener("click", async () => {
  const body = settings.installed
    ? "The old install command, the agent key and the control key of this host are revoked at once. The agent on the machine stops being accepted until you run the new command there. The host's stored data is kept."
    : "The old install command stops working at once. The host has no keys yet, so none are revoked.";
  const ok = await confirmDialog({ title: `Regenerate the install command for ${host}?`, body, confirmText: "Regenerate", danger: true });
  if (!ok) return;
  $("regen").disabled = true;
  let needsUrl = false;
  try {
    const path = settings.installed ? "/enrolment/reissue" : "/enrolment/regenerate";
    const made = await api("POST", hostUrl(path), csrf, { confirmed: true, pool: $("pool").value.trim() });
    shown = { kind: "install", made };
    install = null;
    await reload(true);
  } catch (err) {
    if (err.code === NEEDS_URL) needsUrl = true;
    else if (err.message !== "not signed in") showError(msg, err.message);
  } finally {
    $("regen").disabled = false;
  }
  // No Observe address is configured or saved yet: ask once, then make the command.
  if (needsUrl) { await askPublicUrl(urlParts, csrf); $("regen").click(); }
});

$("cleanup").addEventListener("click", async () => {
  const body = `This makes a command to run on the machine that holds the install made for ${host}, for example one that was run on the wrong machine. It removes the agent container and settings, and the control service, its rules and its account. It refuses any machine with no install made for ${host}. Data volumes are kept.`;
  const ok = await confirmDialog({ title: `Clean up a machine for ${host}?`, body, confirmText: "Make the command", danger: true });
  if (!ok) return;
  try {
    const made = await api("POST", hostUrl("/tasks"), csrf, { kind: "cleanup", confirmed: true });
    shown = { kind: "cleanup", made };
    install = null;
    await reload(false);
  } catch (err) {
    if (err.message !== "not signed in") showError(msg, err.message);
  }
});

// ---- danger zone ----
$("revoke").addEventListener("click", async () => {
  const ok = await typedConfirm({
    title: `Revoke the keys of ${host}?`, name: host, confirmText: "Revoke keys",
    body: "The host's agent and control service are no longer accepted. They stay revoked until you regenerate the install command and run it on the host.",
  });
  if (!ok) return;
  try {
    const out = await api("POST", hostUrl("/keys/revoke"), csrf, { confirm_host: host });
    toast(`${out.keys_revoked} key${out.keys_revoked === 1 ? "" : "s"} revoked.`, "up");
    await reload(false);
  } catch (err) {
    if (err.message !== "not signed in") showError(msg, err.message);
  }
});

$("remove").addEventListener("click", async () => {
  const ok = await typedConfirm({
    title: `Remove ${host}?`, name: host, confirmText: "Remove host",
    body: "The host's keys are revoked and its enrolment and stored data are deleted from Observe. The audit log is kept.",
  });
  if (!ok) return;
  try {
    await api("POST", hostUrl("/remove"), csrf, { confirm_host: host });
    toast("Host removed.", "up");
    window.location.assign("/");
  } catch (err) {
    if (err.message !== "not signed in") showError(msg, err.message);
  }
});

// ---- loading and polling ----
function stopPoll() {
  if (pollHandle) { pollHandle.stop(); pollHandle = null; }
}

async function load() {
  settings = await api("GET", hostRead("/settings"));
  drawIdentity();
  drawInstallCard();
  drawCommand();
  drawAllowlistState();
}

async function reload(resetDraft) {
  await load();
  if (resetDraft || !draft) draft = draftFromAllowlist(settings.allowlist);
  drawAllowlist();
  poll();
}

// Poll while an update or cleanup is waiting or running, or an install command is being watched.
// The poller waits for a read to finish before it plans the next, and a read that was in flight
// when a newer poller started does nothing.
function poll() {
  stopPoll();
  const handle = poller(async ({ signal }) => {
    const watching = !!shown && shown.kind === "install" && !shown.finished;
    if (!shouldPoll(settings, watching)) { handle.stop(); return; }
    const [next, prog] = await Promise.all([
      api("GET", hostRead("/settings")),
      watching ? api("GET", hostRead("/enrolment")) : Promise.resolve(null),
    ]);
    if (signal.aborted) return;
    settings = next;
    if (prog) install = prog;
    drawIdentity();
    drawInstallCard();
    drawCommand();
    drawAllowlistState();
    if (prog && (prog.ready || prog.expired)) { shown.finished = true; handle.stop(); }
  }, {
    interval: POLL_MS, delay: POLL_MS,
    errorHandler: (err) => {
      if (err.message === "not signed in") handle.stop();
      else showError(msg, err.message);
    },
  });
  pollHandle = handle;
}

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin($("page"), "Host settings"); return; }
  csrf = me.csrf;
  if (!host) { showError(msg, "This address does not name a host."); return; }
  try {
    await reload(true);
  } catch (err) {
    if (err.message === "unknown host") showError(msg, `Observe does not know a host named ${host}.`);
    else if (err.message !== "not signed in") showError(msg, err.message);
  }
})();

window.addEventListener("pagehide", stopPoll);
