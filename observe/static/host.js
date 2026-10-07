// Per-host hardware page. Every string came from a host agent, so it is written with
// textContent only, never as markup. The Control section is a separate module that mounts
// itself into its own card, so a refresh of this page never touches it.
import { el } from "/static/js/dom.js";
import { statusChip } from "/static/js/chips.js";

const SECTIONS = [
  ["cpu", "CPU"], ["memory", "Memory"], ["power", "Power"], ["temperatures", "Temperatures"],
  ["fans", "Fans and controller"], ["raid", "RAID"], ["zfs", "ZFS pools"], ["disks", "Disks"],
  ["ups", "UPS"], ["ha", "Home Assistant"], ["containers", "Containers"],
  ["integrations", "Integration health"], ["repairs", "Repairs"], ["backups", "Backups"],
  ["network", "Network interfaces"],
  ["alerts", "Alerts"],
];
const KPI_SECTIONS = ["cpu", "temperatures", "fans", "disks"];
const STATE_TEXT = {
  stale: "stale", unavailable: "source unavailable", absent: "not present on this host",
  not_reported: "never reported",
};
const STATUS_STATE = { good: "up", warning: "warn", critical: "down" };
const page = document.getElementById("page");
const name = new URLSearchParams(location.search).get("name") || "";
let isAdmin = false;

function chip(status, text) {
  return statusChip(STATUS_STATE[status] || status, text);
}

function ago(s) {
  if (s == null) return "never";
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function fmtTime(ts) {
  return new Date((typeof ts === "number" ? ts : Date.parse(ts) / 1000) * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
}

function fmtValue(i) {
  if (i.value == null) return "no value";
  const v = Math.abs(i.value) >= 1e6 ? i.value.toExponential(3)
    : Math.abs(i.value) >= 100 ? Math.round(i.value) : Math.round(i.value * 100) / 100;
  return `${v}${i.unit ? " " + i.unit : ""}`;
}

function labelText(labels) {
  return Object.entries(labels || {}).map(([k, v]) => `${k}=${v}`).join(" ");
}

function dataTable(heads, rows) {
  const wrap = el("div", "table-wrap");
  const t = el("table", "data");
  const thead = el("thead");
  const hr = el("tr");
  for (const h of heads) hr.append(el("th", null, h));
  thead.append(hr);
  const tbody = el("tbody");
  for (const cells of rows) {
    const r = el("tr");
    for (const c of cells) {
      const td = el("td");
      if (c instanceof Node) td.append(c); else td.textContent = c;
      r.append(td);
    }
    tbody.append(r);
  }
  t.append(thead, tbody);
  wrap.append(t);
  return wrap;
}

function itemsTable(items) {
  return dataTable(["Reading", "Labels", "Value", "State", "Seen"], items.map((i) => {
    const st = el("span");
    st.append(chip(i.status));
    if (i.reason) st.append(" ", el("span", "muted", i.reason));
    return [`${i.source}.${i.metric}`, labelText(i.labels), fmtValue(i), st,
      `${ago(i.age_seconds)}${i.stale ? " (stale)" : ""}`];
  }));
}

function eventsList(events) {
  const ol = el("ol", "host-events");
  for (const e of events) {
    const li = el("li");
    li.append(el("span", "when", fmtTime(e.ts)), ` ${e.severity} ${e.kind}: ${e.title}`);
    ol.append(li);
  }
  return ol;
}

function card(title, ...chips) {
  const d = el("details", "card");
  d.open = true;
  const s = el("summary");
  s.append(el("span", null, title), ...chips);
  d.append(s);
  return d;
}

function section(title, sec, isEvents) {
  const box = card(title, chip(sec.status));
  if (sec.state !== "ok") box.firstChild.append(chip(sec.state, STATE_TEXT[sec.state] || sec.state));
  if (sec.note) box.append(el("p", "card-sub", sec.note));
  if (sec.items.length) box.append(isEvents ? eventsList(sec.items) : itemsTable(sec.items));
  return box;
}

function sourcesTable(sources) {
  const box = card("Sources");
  if (!sources.length) {
    box.append(el("p", "card-sub", "No source has reported."));
    return box;
  }
  box.append(dataTable(["Source", "State", "Reason", "Reported"], sources.map((s) => {
    const state = !s.present ? "absent" : !s.available ? "unavailable" : s.stale ? "stale" : "ok";
    const st = el("span");
    st.append(chip(s.status), " ", el("span", "muted", STATE_TEXT[state] || "available"));
    return [s.source, st, s.present ? s.reason : "", ago(s.age_seconds)];
  })));
  return box;
}

function kpis(h) {
  const row = el("div", "kpi-row");
  for (const key of KPI_SECTIONS) {
    const sec = h[key];
    const title = SECTIONS.find(([k]) => k === key)[1];
    const tile = el("div", "kpi");
    tile.append(el("span", "kpi-label", title), el("span", "kpi-value", String(sec.items.length)));
    const foot = el("span", "kpi-label");
    foot.append(chip(sec.status));
    tile.append(foot);
    row.append(tile);
  }
  return row;
}

function notice(h) {
  if (h.status === "good" || !h.status_reason) return null;
  const n = el("section", "card notice");
  n.setAttribute("role", "status");
  n.append(el("h3", null, h.status === "critical" ? "Needs attention" : "Warning"),
    el("p", null, h.status_reason));
  return n;
}

function render(h) {
  document.title = `${h.host} - Observe`;
  document.getElementById("summary").replaceChildren(chip(h.status, `${h.host}: ${h.status}`));
  const frag = document.createDocumentFragment();
  const head = el("div", "host-title");
  const crumb = el("p", "card-sub");
  const back = el("a", null, "Hosts");
  back.href = "/";
  crumb.append(back, " / ", h.host);
  const line = el("h1");
  line.append(h.host, " ", chip(h.status));
  head.append(crumb, line);
  if (isAdmin) {
    // The settings page needs an admin session; a viewer would only see a refusal there.
    const settings = el("a", "btn", "Settings");
    settings.href = `/hosts/${encodeURIComponent(h.host)}/settings`;
    head.append(settings);
  }
  head.append(el("p", "card-sub",
    `${h.platform || "unknown platform"}, agent ${h.agent_version || "unknown"}. ` +
    (h.heard ? `Last report ${ago(h.age_seconds)}.` : "No batch has ever arrived.")));
  if (!h.monitored) {
    head.append(el("p", "card-sub", "Not listed as a pushed_host monitor, so it never alerts."));
  } else if (h.monitor) {
    head.append(el("p", "card-sub", `Monitor ${h.monitor.name}: ${h.monitor.effective_state}.`));
  }
  const b = h.boot;
  if (b.boot_ts) {
    const prev = b.clean_shutdown === true ? "previous shutdown was clean"
      : b.clean_shutdown === false ? "previous shutdown was a crash" : "previous shutdown unknown";
    head.append(el("p", "card-sub", `Last boot ${fmtTime(b.boot_ts)}: ${prev}`));
  }
  frag.append(head, kpis(h));
  const n = notice(h);
  if (n) frag.append(n);
  for (const [key, title] of SECTIONS) frag.append(section(title, h[key], key === "alerts"));
  const crashes = h.events.filter((e) => e.kind.startsWith("boot.") && !e.kind.startsWith("boot.clean"));
  if (crashes.length) {
    const cr = card("Crash events");
    cr.append(eventsList(crashes));
    frag.append(cr);
  }
  const ev = card("Recent events");
  ev.append(h.events.length ? eventsList(h.events) : el("p", "card-sub", "No events reported."));
  frag.append(ev, sourcesTable(h.sources));
  page.replaceChildren(frag);
  document.getElementById("footer").textContent = `refreshed ${new Date().toLocaleTimeString()}`;
}

let refreshing = false;

// A refresh never starts while the previous one is still running, so a slow server is
// not given a growing queue of overlapping requests.
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const r = await fetch(`/api/v2/hosts/${encodeURIComponent(name)}`);
    if (r.status === 401) { location.assign("/login"); return; }
    if (r.status === 404) { page.replaceChildren(el("p", null, "Unknown host.")); return; }
    if (r.ok) render(await r.json());
  } catch (_) {
    document.getElementById("footer").textContent = "observe unreachable, retrying";
  } finally {
    refreshing = false;
  }
}

async function start() {
  try {
    const r = await fetch("/api/session");
    if (r.ok) isAdmin = !!(await r.json()).is_admin;
  } catch (_) { /* a viewer view is the safe default */ }
  refresh();
  setInterval(refresh, 10000);
}

start();
