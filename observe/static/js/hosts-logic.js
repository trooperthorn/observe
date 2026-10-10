// Row logic for the Hosts list (/hosts): pure functions over the items of GET /api/v2/hosts and
// GET /api/v2/waiting-hosts, so tests/js/hosts.test.mjs can run them without a browser. No DOM
// here; hosts.js turns each row into nodes with textContent only.

// A host's overall status is good, warning or critical (observe/hostview.py). An enrolled host
// that has not reported yet has no view, so it is shown as waiting. Each maps to a chip state.
export const STATUS_STATE = { good: "up", warning: "warn", critical: "down", waiting: "pending" };
const STATUS_RANK = { critical: 0, warning: 1, good: 2, waiting: 3 };

export function ageText(s) {
  if (s === null || s === undefined) return "never";
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function hostHref(name) {
  return `/host?name=${encodeURIComponent(name)}`;
}

export function settingsHref(name) {
  return `/hosts/${encodeURIComponent(name)}/settings`;
}

function rank(status) {
  return Object.prototype.hasOwnProperty.call(STATUS_RANK, status) ? STATUS_RANK[status] : 4;
}

// One table row from a host list item. `monitored` says whether the host is listed as a
// pushed_host monitor in the YAML, which is what lets it alert; the list only reports it.
export function hostRow(h) {
  return {
    name: h.host, platform: h.platform || "", status: h.status, statusRank: rank(h.status),
    age: h.heard ? h.age_seconds : null, stale: !!h.stale, agent: h.agent_version || "",
    monitored: !!h.monitored, monitor: h.monitor && h.monitor.name ? h.monitor.name : "",
    detail: h.status_reason || "", href: hostHref(h.host), settings: settingsHref(h.host),
    waiting: false,
  };
}

// One table row from a waiting host. No host page exists for it yet, so its only link is the
// enrolment page the API names, which needs an admin session.
export function waitingRow(w) {
  return {
    name: w.host, platform: w.platform || "", status: "waiting", statusRank: rank("waiting"),
    age: null, stale: false, agent: "", monitored: false, monitor: "", detail: w.note || "",
    href: null, settings: w.enrolment_url || settingsHref(w.host), waiting: true,
  };
}

// Every host, then every waiting host the list does not already name.
export function hostRows(hosts, waiting) {
  const rows = (hosts || []).map(hostRow);
  const seen = new Set(rows.map((r) => r.name));
  for (const w of waiting || []) {
    if (seen.has(w.host)) continue;
    seen.add(w.host);
    rows.push(waitingRow(w));
  }
  return rows;
}

// The rows whose name or platform contains the search text, case-insensitively.
export function filterRows(rows, q) {
  const text = (q || "").trim().toLowerCase();
  if (!text) return rows;
  return rows.filter((r) => r.name.toLowerCase().includes(text)
    || r.platform.toLowerCase().includes(text));
}

// The header line: how many hosts, how many need attention, how many wait for first data.
export function summaryText(rows) {
  const n = rows.length;
  const bad = rows.filter((r) => r.status === "critical" || r.status === "warning").length;
  const waiting = rows.filter((r) => r.waiting).length;
  const parts = [`${n} host${n === 1 ? "" : "s"}`];
  if (bad) parts.push(`${bad} need${bad === 1 ? "s" : ""} attention`);
  if (waiting) parts.push(`${waiting} waiting for first data`);
  return parts.join(", ");
}

// "1 host", "3 hosts": a count with its noun.
export function plural(n, noun) {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

// How many hosts the Hosts list shows as Down: the same verdict (status critical) the list,
// the host page and the pushed_host monitor all read.
export function downHostCount(hosts) {
  return (hosts || []).filter((h) => h.status === "critical").length;
}

// The sub-label of the dashboard's Down tile. `hosts` is the Hosts list, or null when this
// viewer may not read it; the count then falls back to Down pushed_host monitors.
export function downTileLabel(hosts, monitors) {
  const n = hosts ? downHostCount(hosts)
    : (monitors || []).filter((m) => m.effective_state === "down" && m.type === "pushed_host").length;
  return n ? `${plural(n, "host")} down` : "";
}
