// Add host wizard: five steps on one page, the step kept in the URL hash. The server makes the
// command and the token; this page only shows them. The command lives in memory only. It never
// goes into the URL, storage or a toast. Everything from the server is written with textContent.
import { el, clear } from "/static/js/dom.js";
import { api, whoami } from "/static/js/api.js";
import { statusChip } from "/static/js/chips.js";
import { notAdmin, showError, copyText } from "/static/js/admin-ui.js";
import { confirmDialog } from "/static/js/dialog.js";
import { NEEDS_URL, askPublicUrl } from "/static/js/public-url.js";
import {
  STEPS, controlBlock, defaultFans, defaultServices, platformNote, validName,
  validHeader, validService, buildBody, stepAnnouncement, stepFromHash, progressChip, reportChip, hostHref,
  noticeFor, guardText,
} from "/static/js/wizard-logic.js";

const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);
const msg = $("msg");
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "");

let csrf = "";
let known = new Set();
let created = null;   // { host, platform, platform_label, expires_at, command }
let latest = null;    // the last progress response
let pollGen = 0;
let current = "host";
let state = fresh();

// The confirm-the-address box: the install command never uses the address this page was reached at.
const urlParts = { box: $("public-url-box"), form: $("public-url-form"), input: $("public-url"), status: $("public-url-status") };

function fresh() {
  return { name: "", platform: "linux", pool: "", control: false, reboot: false,
           fans: defaultFans("linux"), services: defaultServices() };
}

// ---- step display ----
// moveFocus is true when the person changed the step: focus goes to its heading and a live region
// says which step it is, so a keyboard or screen reader user is not left on a hidden control.
function show(step, moveFocus) {
  current = step;
  for (const s of STEPS) $(`step-${s}`).hidden = s !== step;
  const at = STEPS.indexOf(step);
  for (const li of $("steps").children) {
    const i = STEPS.indexOf(li.dataset.step);
    li.classList.toggle("done", i < at);
    if (i === at) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  }
  showError(msg, "");
  if (step === "install") drawInstall();
  if (step === "live") drawLive();
  if (step === "install" || step === "live") poll(); else stopPoll();
  drawNotice();
  if (moveFocus) {
    $("step-announce").textContent = stepAnnouncement(step);
    $(`step-${step}-h`).focus();
  }
}

// The command's own state on the install and live steps: expired, already used, or refused by the
// script's guard on the machine it ran on (the command stays valid then).
function drawNotice() {
  const watching = !!created && (current === "install" || current === "live");
  const notice = watching ? noticeFor(latest) : null;
  $("expired").hidden = !notice;
  if (notice) {
    $("expired-h").textContent = notice.title;
    $("expired-text").textContent = notice.text;
    $("regen").textContent = notice.button;
  }
  const refused = watching ? guardText(latest) : "";
  $("guard").hidden = !refused;
  if (refused) $("guard-text").textContent = `${refused} This command was made for ${created.host}.`;
}

function go(step) {
  if (window.location.hash === `#${step}`) show(step, true); else window.location.hash = `#${step}`;
}

function onHash() { show(stepFromHash(window.location.hash, created), true); }

// ---- step 1: host ----
function platform() { return document.querySelector('input[name="platform"]:checked').value; }

function checkName() {
  const name = $("host-name").value.trim();
  const out = $("name-status");
  if (!name) { out.textContent = ""; return false; }
  if (!validName(name)) { out.textContent = "That name is not valid."; return false; }
  if (known.has(name)) { out.textContent = "A host with this name already exists."; return false; }
  out.textContent = "That name is free.";
  return true;
}

function applyPlatform() {
  const p = platform();
  if (p !== state.platform) {
    state.platform = p;
    state.fans = defaultFans(p);
    state.services = defaultServices();
    state.control = false;
  }
  $("pool-field").hidden = p !== "truenas";
  $("platform-note").textContent = platformNote(p);
}

$("form-host").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!checkName()) { $("host-name").focus(); return; }
  state.name = $("host-name").value.trim();
  state.pool = $("pool").value.trim();
  applyPlatform();
  const reason = controlBlock(state.platform);
  $("control-on").disabled = !!reason;
  $("control-on").checked = !reason && state.control;
  $("control-reason").textContent = reason;
  go("agent");
});
$("host-name").addEventListener("input", checkName);
for (const r of document.querySelectorAll('input[name="platform"]')) r.addEventListener("change", applyPlatform);

// ---- step 2: agent and control ----
$("agent-back").addEventListener("click", () => go("host"));
$("form-agent").addEventListener("submit", (e) => {
  e.preventDefault();
  state.control = $("control-on").checked && !$("control-on").disabled;
  if (state.control) { drawAllowlist(); go("allowlist"); } else { create(); }
});

// ---- step 3: allowlist ----
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
  clear($("fan-list")).append(...state.fans.map((f) => listItem(f, true)));
  clear($("service-list")).append(...state.services.map((s) => listItem(s, false)));
  $("reboot").checked = state.reboot;
  $("allow-error").textContent = "";
}

function addEntry(inputId, list, ok, what) {
  const input = $(inputId);
  const name = input.value.trim();
  if (!name) return;
  if (!ok(name)) { $("allow-error").textContent = `That ${what} has characters that are not allowed.`; return; }
  $("allow-error").textContent = "";
  if (!list.some((x) => x.name === name)) list.push({ name, on: true, limit: "" });
  input.value = "";
  drawAllowlist();
}

$("fan-add").addEventListener("click", () => addEntry("fan-new", state.fans, validHeader, "header"));
$("service-add").addEventListener("click", () => addEntry("service-new", state.services, validService, "service name"));
$("reboot").addEventListener("change", () => { state.reboot = $("reboot").checked; });
$("allow-back").addEventListener("click", () => go("agent"));
$("form-allow").addEventListener("submit", (e) => { e.preventDefault(); create(); });

// ---- create ----
async function create() {
  const built = buildBody(state);
  if (built.error) { showError(msg, built.error); return; }
  for (const id of ["create", "agent-next"]) $(id).disabled = true;
  let needsUrl = false;
  try {
    created = await api("POST", "/api/hosts", csrf, built.body);
    latest = null;
    known.add(created.host);
    go("install");
  } catch (e) {
    if (e.code === NEEDS_URL) needsUrl = true;
    else if (e.message !== "not signed in") showError(msg, e.message);
  } finally {
    for (const id of ["create", "agent-next"]) $(id).disabled = false;
  }
  // No Observe address is configured or saved yet: ask once, then make the host.
  if (needsUrl) { await askPublicUrl(urlParts, csrf); create(); }
}

// ---- step 4: install ----
function drawInstall() {
  if (!created) return;
  $("install-sub").textContent = `${created.host} · ${created.platform_label}`;
  $("cmd").textContent = created.command;
  $("expiry").textContent = `Expires at ${when(created.expires_at)} (${Math.round(created.ttl_s / 60)} minutes) and works once.`;
  drawNotice();
}

$("copy").addEventListener("click", () => { if (created) copyText(created.command, $("cmd")); });
$("ran").addEventListener("click", () => go("live"));

// Regenerate: before the command was used it replaces the token; after, the old keys are revoked
// as well, so that asks first.
$("regen").addEventListener("click", async () => {
  if (!created) return;
  const used = !!latest && latest.token_state === "used";
  if (used) {
    const ok = await confirmDialog({
      title: `Regenerate the install command for ${created.host}?`,
      body: "The old command, and the agent and control keys it made, stop working at once. The agent on the machine is not accepted until the new command is run there. The host's stored data is kept.",
      confirmText: "Regenerate", danger: true,
    });
    if (!ok) return;
  }
  $("regen").disabled = true;
  let needsUrl = false;
  try {
    const path = used ? "reissue" : "regenerate";
    const made = await api("POST", `/api/hosts/${encodeURIComponent(created.host)}/enrolment/${path}`,
                           csrf, { pool: state.pool, confirmed: true });
    created = made;
    latest = null;
    $("expired").hidden = true;
    go("install");
  } catch (e) {
    if (e.code === NEEDS_URL) needsUrl = true;
    else if (e.message !== "not signed in") showError(msg, e.message);
  } finally {
    $("regen").disabled = false;
  }
  if (needsUrl) { await askPublicUrl(urlParts, csrf); $("regen").click(); }
});

// ---- step 5: live ----
function drawLive() {
  if (!created) return;
  $("live-sub").textContent = `Watching ${created.host}. This page checks every ${POLL_MS / 1000} seconds.`;
  $("open-host").href = hostHref(created.host);
  drawProgress();
}

function drawProgress() {
  const list = clear($("progress"));
  const reports = clear($("reports"));
  if (!latest) return;
  for (const s of latest.steps) {
    const [chipState, word] = progressChip(s.status);
    const li = el("li", s.status);
    li.append(el("span", "p-label", s.label), statusChip(chipState, word));
    if (s.at) li.append(el("span", "p-time", when(s.at)));
    list.append(li);
  }
  for (const r of latest.install || []) {
    const [chipState, word] = reportChip(r.status);
    const li = el("li");
    li.append(el("span", "p-label", `Install step: ${r.step}`), statusChip(chipState, word));
    if (r.at) li.append(el("span", "p-time", when(r.at)));
    if (r.note) li.append(el("span", "p-note", r.note));
    reports.append(li);
  }
  $("done").hidden = !latest.ready;
  drawNotice();
}

// Poll while step 4 or 5 is showing, and stop when the host is ready or the command expired.
// A generation counter makes a poll that was in flight when the page left a step do nothing.
function stopPoll() { pollGen += 1; }

function poll() {
  if (!created) return;
  pollGen += 1;
  const gen = pollGen;
  const host = created.host;
  const tick = async () => {
    try {
      const next = await api("GET", `/api/hosts/${encodeURIComponent(host)}/enrolment`);
      if (gen !== pollGen) return;
      latest = next;
      drawProgress();
      if (next.ready || next.expired) return;
    } catch (e) {
      if (gen !== pollGen) return;
      if (e.message === "not signed in") return;
      showError(msg, e.message);
    }
    setTimeout(tick, POLL_MS);
  };
  tick();
}

$("again").addEventListener("click", () => {
  stopPoll();
  created = null;
  latest = null;
  state = fresh();
  $("host-name").value = "";
  $("pool").value = "";
  $("name-status").textContent = "";
  document.querySelector('input[name="platform"][value="linux"]').checked = true;
  applyPlatform();
  go("host");
});

// ---- start ----
(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin($("page"), "Adding a host"); return; }
  csrf = me.csrf;
  try {
    const list = await api("GET", "/api/hosts");
    known = new Set(list.hosts.map((h) => h.host || h.name));
  } catch (e) {
    if (e.message !== "not signed in") showError(msg, e.message);
  }
  window.addEventListener("hashchange", onHash);
  applyPlatform();
  const step = stepFromHash(window.location.hash, created);
  if (window.location.hash !== `#${step}` && window.location.hash) {
    history.replaceState(null, "", `#${step}`);
  }
  show(step);
})();
