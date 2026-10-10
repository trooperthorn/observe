// Per-host hardware page. Every string came from a host agent, so it is written with
// textContent only, never as markup. The Control section is a separate module that mounts
// itself into its own card, so a refresh of this page never touches it.
import { el } from "/static/js/dom.js";
import { statusChip } from "/static/js/chips.js";
import { api, get, poller, whoami } from "/static/js/api.js";
import { toast } from "/static/js/toast.js";
import { formatValue, formatWhen, readingText, refreshedText } from "/static/js/format.js";

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
// The host is the path after /hosts/ (the old /host?name= form redirects here).
const PATH_NAME = location.pathname.startsWith("/hosts/")
  ? decodeURIComponent(location.pathname.slice("/hosts/".length)) : "";
const name = PATH_NAME || new URLSearchParams(location.search).get("name") || "";
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
  return formatWhen(ts);
}

function fmtValue(i) {
  return readingText(i);
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
    const r = el("tr", cells.cls || null);
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
  const heads = ["Reading", "Labels", "Value", "State", "Seen"];
  if (isAdmin) heads.push("");
  return dataTable(heads, items.map((i) => {
    const st = el("span");
    if (i.ignored) st.append(chip("no_data", "Ignored"));
    else st.append(chip(i.status));
    if (i.reason) st.append(" ", el("span", "muted", i.reason));
    const row = [`${i.source.replace(/^hostwatch\.collector\./, "")}.${i.metric}`, labelText(i.labels), fmtValue(i), st,
      `${ago(i.age_seconds)}${i.stale ? " (stale)" : ""}`];
    if (isAdmin) row.push(ignoreButton(i));
    if (i.ignored) row.cls = "ignored";
    return row;
  }));
}

// Every hw.id ignored on this host, read back from the items the server marked.
let ignoredIds = new Set();

function collectIgnored(h) {
  const out = new Set();
  for (const [key] of SECTIONS) {
    for (const i of (h[key] && h[key].items) || []) {
      if (i.ignored && i.labels && i.labels["hw.id"]) out.add(i.labels["hw.id"]);
    }
  }
  return out;
}

// An admin can ignore a reading that has a hw.id (a floating sensor input, an empty fan
// header). It stays on the page, greyed out, and stops counting toward the host's state. The
// change is saved on the server and audited.
function ignoreButton(i) {
  const id = i.labels && i.labels["hw.id"];
  if (!id) return "";
  const b = el("button", "btn", i.ignored ? "Count again" : "Ignore");
  b.type = "button";
  b.setAttribute("aria-label", `${i.ignored ? "Count again" : "Ignore"} ${id}`);
  b.addEventListener("click", async () => {
    const next = new Set(ignoredIds);
    if (i.ignored) next.delete(id); else next.add(id);
    b.disabled = true;
    try {
      await api("PUT", `/api/hosts/${encodeURIComponent(name)}/ignored`, null,
        { ignored: [...next] });
      toast(i.ignored ? `${id} counts again` : `${id} is ignored on this host`);
      await refresh();
    } catch (err) {
      toast(`Not saved: ${err.message}`, "error");
      b.disabled = false;
    }
  });
  return b;
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
  d.dataset.title = title;
  const s = el("summary");
  s.append(el("span", null, title), ...chips);
  d.append(s);
  return d;
}

// Sections with nothing on this host (absent hardware, never reported) start collapsed, so the
// page leads with what the host has. A section the visitor opened or closed keeps that choice.
const NO_DATA_STATES = new Set(["absent", "not_reported"]);
const toggled = new Map();

function section(title, sec, isEvents) {
  const box = card(title, chip(sec.status));
  box.open = toggled.has(title) ? toggled.get(title) : !NO_DATA_STATES.has(sec.state);
  box.addEventListener("toggle", () => toggled.set(title, box.open));
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
  if (!h.heard) return document.createDocumentFragment();  // nothing to count yet
  const row = el("div", "kpi-row");
  for (const key of KPI_SECTIONS) {
    const sec = h[key];
    const title = SECTIONS.find(([k]) => k === key)[1];
    const tile = el("div", "kpi");
    const n = sec.items.length;
    tile.append(el("span", "kpi-label", title), el("span", "kpi-value", String(n)),
      el("span", "kpi-label", n === 1 ? "reading" : "readings"));
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

// The monitor reads the same verdict as this page (observe/hostview.py), but changes state only
// after its failures_to_down confirmation, so a fresh change is said to be pending, never shown
// as a contradiction.
const VERDICT_STATE = { good: "up", warning: "warn", critical: "down" };
function monitorLine(h) {
  const m = h.monitor;
  const want = VERDICT_STATE[h.status];
  const shown = m.effective_state;
  if (!want || shown === want) return `Monitor ${m.name}: ${shown}.`;
  if (m.blocked_by) return `Monitor ${m.name}: ${shown}, held by ${m.blocked_by}.`;
  return `Monitor ${m.name}: ${shown}, changing to ${want} once the next polls confirm it.`;
}

// The components the pushed_host monitor lists in the YAML, graded by the same function as the
// monitor itself.
function componentsCard(sec) {
  if (!sec || !sec.items || !sec.items.length) return null;
  const box = card("Monitor components", chip(sec.status));
  box.append(dataTable(["Component", "Status", "Reason"], sec.items.map((i) => {
    const st = el("span");
    st.append(chip(i.status), i.stale ? " stale" : "");
    return [i.name, st, i.reason || ""];
  })));
  return box;
}

function render(h) {
  document.title = `${h.host} - Observe`;
  ignoredIds = collectIgnored(h);
  const frag = document.createDocumentFragment();
  // The same title block as the other pages: crumbs, then the title row with its action.
  const head = el("div", "admin-title host-title");
  const crumb = el("p", "crumbs muted");
  const back = el("a", null, "Hosts");
  back.href = "/hosts";
  crumb.append(back, " / ", h.host);
  const titleRow = el("div", "title-row");
  const line = el("h1");
  line.append(h.host, " ", chip(h.status));
  titleRow.append(line);
  if (isAdmin) {
    // The settings page needs an admin session; a viewer would only see a refusal there.
    const settings = el("a", "btn", "Settings");
    settings.href = `/hosts/${encodeURIComponent(h.host)}/settings`;
    titleRow.append(settings);
  }
  head.append(crumb, titleRow);
  head.append(el("p", "card-sub",
    `${h.platform || "unknown platform"}, ` +
    `${h.agent_label || `agent ${h.agent_version || "unknown"}`}. ` +
    (h.heard ? `Last report ${ago(h.age_seconds)}.` : "No batch has ever arrived.")));
  if (!h.monitored) {
    head.append(el("p", "card-sub", "Not listed as a pushed_host monitor, so it never alerts."));
  } else if (h.monitor) {
    head.append(el("p", "card-sub", monitorLine(h)));
  }
  if (h.install_problem) {
    const p = h.install_problem;
    head.append(el("p", "card-sub row-note",
      `The console install reported step ${p.step} ${p.status}${p.note ? `: ${p.note}` : ""}. ` +
      "It clears when a rerun of the install passes that step."));
  }
  if (h.agent_drops) head.append(el("p", "card-sub row-note", `${h.agent_drops.text}: ` +
    "its outbox is over its limit. This clears when the count stops growing."));
  const b = h.boot;
  if (b.boot_ts) {
    const prev = b.clean_shutdown === true ? "previous shutdown was clean"
      : b.clean_shutdown === false ? "previous shutdown was a crash" : "previous shutdown unknown";
    head.append(el("p", "card-sub", `Last boot ${fmtTime(b.boot_ts)}: ${prev}`));
  }
  frag.append(head, kpis(h));
  const n = notice(h);
  if (n) frag.append(n);
  const comps = componentsCard(h.components);
  if (comps) frag.append(comps);
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
  document.getElementById("footer").textContent = refreshedText();
}

// The poller waits for a refresh to finish before it plans the next one, and runs again when the
// host data moves, so a slow server is never given a growing queue of overlapping requests.
async function refresh() {
  try {
    render(await get(`/api/v2/hosts/${encodeURIComponent(name)}`));
  } catch (e) {
    if (e.status === 401) return;  // the client is already sending the visitor to sign in
    if (e.status === 404) { page.replaceChildren(el("p", null, "Unknown host.")); return; }
    document.getElementById("footer").textContent = "observe unreachable, retrying";
    throw e;
  }
}

async function start() {
  try {
    isAdmin = !!(await whoami()).is_admin;
  } catch (_) { /* a viewer view is the safe default */ }
  poller(refresh, { interval: 10000, domains: ["hosts", "ha"] });
}

start();
