// Hosts list page (/hosts): every host that has reported or is listed as a pushed_host monitor,
// from GET /api/v2/hosts, plus the enrolled hosts that have not reported yet, from
// GET /api/v2/waiting-hosts. Every string came from a host agent or the console, so it is
// written with textContent only. The row logic is in js/hosts-logic.js.
import { el } from "/static/js/dom.js";
import { get, getAll, poller, whoami } from "/static/js/api.js";
import { statusChip, neutralChip } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { STATUS_STATE, ageText, filterRows, hostRows, summaryText } from "/static/js/hosts-logic.js";

const summary = document.getElementById("summary");
const footer = document.getElementById("footer");
const search = document.getElementById("q");
let isAdmin = false;
let rows = [];
let table = null;

function muted(text) {
  return el("span", "muted", text);
}

// The host name opens the host page. A waiting host has none yet, so for an admin its name
// opens the enrolment page; a viewer sees plain text.
function nameCell(r) {
  if (r.href || (r.waiting && isAdmin)) {
    const a = el("a", null, r.name);
    a.href = r.href || r.settings;
    return a;
  }
  return el("span", null, r.name);
}

function statusCell(r) {
  return statusChip(STATUS_STATE[r.status] || r.status, r.waiting ? "Waiting" : undefined);
}

// A host that has never reported is simply "never": stale only qualifies a real age.
function ageCell(r) {
  if (r.waiting || r.age === null) return muted("never");
  return `${ageText(r.age)}${r.stale ? " (stale)" : ""}`;
}

// Listed as a pushed_host monitor in the YAML or not. Only a listed host alerts; this page
// reports the fact and changes nothing.
function monitorCell(r) {
  if (!r.monitored) return muted("Not listed");
  const span = el("span");
  span.append(neutralChip("Listed"));
  if (r.monitor && r.monitor !== r.name) span.append(" ", muted(r.monitor));
  return span;
}

function settingsCell(r) {
  const a = el("a", "btn ghost", r.waiting ? "Enrolment" : "Settings");
  a.href = r.settings;
  return a;
}

function detailCell(r) {
  const box = el("span");
  box.append(muted(r.detail));
  for (const note of r.notes || []) box.append(el("span", "row-note", note));
  return box;
}

function columns() {
  const cols = [
    { key: "host", label: "Host", get: (r) => r.name, render: nameCell },
    { key: "platform", label: "Platform", get: (r) => r.platform || null,
      render: (r) => r.platform || muted("unknown") },
    { key: "status", label: "Status", get: (r) => r.statusRank, render: statusCell },
    { key: "age", label: "Last report", numeric: true, get: (r) => r.age, render: ageCell },
    { key: "agent", label: "Agent", get: (r) => r.agent || null,
      render: (r) => r.agent || muted("unknown") },
    { key: "monitor", label: "Monitor", get: (r) => (r.monitored ? 0 : 1), render: monitorCell },
    { key: "detail", label: "Detail", render: detailCell },
  ];
  if (isAdmin) cols.push({ key: "settings", label: "Settings", render: settingsCell });
  return cols;
}

function emptyNote() {
  const p = el("span", null, "No host has reported and none is waiting to.");
  if (isAdmin) {
    const a = el("a", null, "Add host");
    a.href = "/hosts/new";
    p.append(" ", a, " enrols one.");
  }
  return p;
}

function draw() {
  table.setRows(filterRows(rows, search.value));
  summary.replaceChildren(el("span", "muted", summaryText(rows)));
}

// The poller waits for a refresh to finish before it plans the next one, and runs again when the
// host data moves, so a slow server is never given a growing queue of overlapping requests.
async function refresh() {
  try {
    const [hosts, waiting] = await Promise.all([
      getAll("/api/v2/hosts"), get("/api/v2/waiting-hosts")]);
    rows = hostRows(hosts, waiting.items);
    draw();
    footer.textContent = `refreshed ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    if (e.status === 401) return;  // the client is already sending the visitor to sign in
    footer.textContent = "observe unreachable, retrying";
    throw e;
  }
}

async function start() {
  try {
    isAdmin = !!(await whoami()).is_admin;
  } catch (_) { /* a viewer view is the safe default */ }
  table = sortableTable({ columns: columns(), rows: [], pageSizes: [25, 100],
                          empty: emptyNote(), caption: "Hosts" });
  document.getElementById("hosts").append(table.root);
  search.addEventListener("input", draw);
  poller(refresh, { interval: 15000, domains: ["hosts"] });
}

start();
