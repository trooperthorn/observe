// Every string that reaches the page came from a monitored device or the
// config (SNMP values, MQTT payloads, HTTP errors). It is written with
// textContent and SVG attributes only, never innerHTML, so a hostile payload
// renders as text.
import { el } from "/static/js/dom.js";
import { statusChip, statusIcon } from "/static/js/chips.js";
import { stateInfo } from "/static/js/chip-states.js";
import { applyLayout, initTiles, setDeclared } from "/static/js/tiles.js";
import { groupTile } from "/static/js/tiles-logic.js";

const ORDER = { down: 0, unreachable: 1, warn: 2, pending: 3, up: 4 };
const expanded = new Set();
const rows = new Map();
const tpl = document.getElementById("row-tpl");
const groupsEl = document.getElementById("groups");
const sectionsEl = document.getElementById("sections");
const stateFilter = document.getElementById("state-filter");
const search = document.getElementById("search");
const closedGroups = new Set();
let lastData = null;

function shown(m) {
  const f = stateFilter.value;
  if (f === "problems" && m.effective_state === "up") return false;
  if (f !== "all" && f !== "problems" && m.effective_state !== f) return false;
  const q = search.value.trim().toLowerCase();
  return !q || `${m.name} ${m.target} ${m.group}`.toLowerCase().includes(q);
}

function setKpi(id, value, sub, alert) {
  const parts = document.getElementById(id).children;
  parts[1].textContent = String(value);
  parts[1].classList.toggle("kpi-alert", Boolean(alert));
  parts[2].textContent = sub;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
}

function fmtVal(m) {
  if (m.result === "fail" && m.value == null) return "";  // a failed probe has no meaningful latency
  if (m.value === null || m.value === undefined) {
    return m.latency_ms != null ? `${Math.round(m.latency_ms)} ms` : "";
  }
  const v = Math.abs(m.value) >= 100 ? Math.round(m.value) : Math.round(m.value * 10) / 10;
  return `${v}${m.unit || ""}`;
}

function makeRow(m) {
  const node = tpl.content.firstElementChild.cloneNode(true);
  const head = node.querySelector(".head");
  head.addEventListener("click", () => {
    const more = node.querySelector(".more");
    const open = more.hidden;
    more.hidden = !open;
    head.setAttribute("aria-expanded", String(open));
    if (open) { expanded.add(m.slug); loadHistory(m.slug, node); } else expanded.delete(m.slug);
  });
  return node;
}

function fcText(f) {
  // Returns [text, css class] for a forecast, or null when there is nothing to say.
  if (!f) return null;
  const low = f.confidence === "low" ? " (low confidence)" : "";
  const when = (ts) => {
    const d = (ts - Date.now() / 1000) / 86400;
    return d <= 0 ? "now" : d < 1 ? `in ${Math.round(d * 24)}h` : `in ~${Math.round(d)}d`;
  };
  if (f.status === "projected" || f.status === "already_crossed") {
    const parts = [];
    if (f.warn_at) parts.push(`warn ${when(f.warn_at)}`);
    if (f.crit_at) parts.push(`crit ${when(f.crit_at)}`);
    const next = Math.min(f.crit_at ?? Infinity, f.warn_at ?? Infinity);
    const days = (next - Date.now() / 1000) / 86400;
    return [`trend: ${parts.join(", ")}${low}`, days <= 1 ? "fc now" : days <= 14 ? "fc soon" : "fc"];
  }
  return [`trend: ${f.status.replace(/_/g, " ")}`, "fc"];
}

function updateRow(node, m) {
  node.className = `mon ${m.effective_state}`;
  node.querySelector(".state-slot").replaceChildren(statusChip(m.effective_state));
  node.querySelector(".name").textContent = m.name;
  node.querySelector(".val").textContent = fmtVal(m);
  node.querySelector(".type").textContent = m.mode ? `${m.type}/${m.mode}` : m.type;
  const st = m.blocked_by ? `unreachable via ${m.blocked_by}` : m.state;
  node.querySelector(".target").textContent =
    `${m.target}  ·  ${st} since ${ago(m.since)}  ·  polled ${ago(m.last_at)}` +
    (m.critical ? "" : "  ·  non-critical");
  const link = node.querySelector(".hostlink");
  link.hidden = m.type !== "pushed_host";
  if (m.type === "pushed_host") link.href = `/host?name=${encodeURIComponent(m.target)}`;
  node.querySelector(".msg").textContent = m.message;
  const fcEl = node.querySelector(".fc");
  const fc = fcText(m.forecast);
  fcEl.textContent = fc ? fc[0] : "";
  fcEl.className = fc ? fc[1] : "fc";
}

function drawSpark(svg, points) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (!points.length) return;
  const ns = "http://www.w3.org/2000/svg";
  const t0 = points[0].ts, t1 = points[points.length - 1].ts || t0 + 1;
  const span = Math.max(1, t1 - t0);
  const x = (t) => ((t - t0) / span) * 300;
  const w = Math.max(1, 300 / points.length);
  for (const p of points) {
    if (p.result === "ok") continue;
    const r = document.createElementNS(ns, "rect");
    r.setAttribute("class", p.result);
    r.setAttribute("x", String(x(p.ts)));
    r.setAttribute("y", "0");
    r.setAttribute("width", String(w));
    r.setAttribute("height", "60");
    svg.appendChild(r);
  }
  const series = points.map((p) => [p.ts, p.value ?? p.latency_ms]).filter((p) => p[1] != null);
  if (series.length < 2) return;
  const vals = series.map((p) => p[1]);
  const lo = Math.min(...vals), hi = Math.max(...vals), range = hi - lo || 1;
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points",
    series.map(([t, v]) => `${x(t).toFixed(1)},${(56 - ((v - lo) / range) * 52).toFixed(1)}`).join(" "));
  svg.appendChild(line);
  const title = document.createElementNS(ns, "title");
  title.textContent = `range ${lo.toFixed(1)} to ${hi.toFixed(1)}`;
  svg.appendChild(title);
}

async function loadHistory(slug, node) {
  try {
    const r = await fetch(`/api/monitors/${encodeURIComponent(slug)}/history?hours=24`);
    if (!r.ok) return;
    const h = await r.json();
    drawSpark(node.querySelector(".spark"), h.points);
    node.querySelector(".avail").textContent = h.availability == null
      ? "no polls in the last 24h"
      : `24h availability ${h.availability}% over ${h.points.length} polls`;
    const list = node.querySelector(".mon-events");
    list.replaceChildren(...h.events.slice(0, 8).map((e) => {
      const li = el("li");
      li.append(el("span", "when", fmtTime(e.ts)), `${e.previous} → ${e.current}: ${e.message}`);
      return li;
    }));
  } catch (_) { /* transient; next refresh retries */ }
}

function render(data) {
  const focused = document.activeElement && document.activeElement.dataset
    ? document.activeElement.dataset.ctl : "";
  drawDashboard(data);
  applyLayout(focused);
}

function drawDashboard(data) {
  const counts = { down: 0, unreachable: 0, warn: 0, pending: 0, up: 0 };
  const groups = new Map();
  for (const m of data.monitors) {
    counts[m.effective_state]++;
    if (!groups.has(m.group)) groups.set(m.group, []);
    groups.get(m.group).push(m);
  }
  const summary = document.getElementById("summary");
  summary.replaceChildren(...Object.entries(counts).filter(([, n]) => n)
    .map(([s, n]) => statusChip(s, `${n} ${stateInfo(s).word}`)));
  document.title = counts.down ? `(${counts.down} down) Overview - Observe` : "Overview - Observe";

  const forecasts = data.monitors.filter((m) => m.forecast && (m.forecast.warn_at || m.forecast.crit_at));
  const week = Date.now() / 1000 + 7 * 86400;
  const soon = forecasts.filter((m) =>
    Math.min(m.forecast.crit_at ?? Infinity, m.forecast.warn_at ?? Infinity) <= week).length;
  const downHosts = data.monitors.filter((m) => m.effective_state === "down" && m.type === "pushed_host").length;
  setKpi("kpi-monitors", data.monitors.length, `${groups.size} groups`);
  setKpi("kpi-down", counts.down, downHosts ? `${downHosts} hosts` : "", counts.down > 0);
  setKpi("kpi-warn", counts.warn, counts.unreachable ? `${counts.unreachable} unreachable` : "");
  setKpi("kpi-capacity", soon, `full within 7 days, ${forecasts.length} forecasts`);
  document.getElementById("tiles").replaceChildren(...[
    ["up", counts.up], ["warn", counts.warn], ["down", counts.down],
    ["unreachable", counts.unreachable], ["pending", counts.pending],
  ].map(([s, n]) => {
    const info = stateInfo(s);
    const tile = el("div", `tile s-${info.role}`);
    tile.append(statusIcon(info.icon), el("span", "tile-count", String(n)), el("span", null, info.word));
    return tile;
  }));

  const frag = document.createDocumentFragment();
  for (const [name, mons] of [...groups.entries()].sort()) {
    mons.sort((a, b) => ORDER[a.effective_state] - ORDER[b.effective_state] ||
      a.name.localeCompare(b.name));
    const visible = mons.filter(shown);
    if (!visible.length) continue;
    const card = el("details", "card");
    card.dataset.tile = groupTile(name);
    card.open = !closedGroups.has(name);
    card.addEventListener("toggle", () => {
      if (card.open) closedGroups.delete(name); else closedGroups.add(name);
    });
    const sum = el("summary");
    sum.append(el("span", "grp-name", name));
    const g = data.groups[name];
    if (g) {
      const chip = statusChip(g.state);
      if (g.worst.length) chip.title = g.worst.join(", ");
      sum.append(chip);
    }
    const per = {};
    for (const m of mons) per[m.effective_state] = (per[m.effective_state] || 0) + 1;
    for (const s of ["down", "unreachable", "warn", "pending", "up"]) {
      if (per[s]) sum.append(statusChip(s, `${per[s]} ${stateInfo(s).word.toLowerCase()}`));
    }
    card.append(sum);
    const ul = el("ul", "group");
    for (const m of visible) {
      let node = rows.get(m.slug);
      if (!node) { node = makeRow(m); rows.set(m.slug, node); }
      updateRow(node, m);
      ul.append(node);
      if (expanded.has(m.slug)) loadHistory(m.slug, node);
    }
    card.append(ul);
    frag.append(card);
  }
  // Group cards from the last render were moved into #sections by applyLayout; drop them.
  for (const old of sectionsEl.querySelectorAll(':scope > [data-tile^="group:"]')) old.remove();
  groupsEl.replaceChildren(frag);
  setDeclared(groups.keys());

  // Capacity outlook: every projected crossing inside the horizon, soonest first.
  const outlook = data.monitors
    .filter((m) => m.forecast && (m.forecast.warn_at || m.forecast.crit_at))
    .map((m) => ({ m, next: Math.min(m.forecast.crit_at ?? Infinity, m.forecast.warn_at ?? Infinity) }))
    .sort((a, b) => a.next - b.next);
  document.getElementById("capacity-panel").hidden = outlook.length === 0;
  document.getElementById("capacity").replaceChildren(...outlook.map(({ m }) => {
    const [text] = fcText(m.forecast);
    const li = el("li");
    li.append(el("span", "when", m.name), `${text} · now ${fmtVal(m)} · ${m.forecast.reason}`);
    return li;
  }));

  const bad = Object.entries(data.alerts).filter(([, a]) => a.last_error);
  document.getElementById("footer").textContent =
    `observe ${data.version} · refreshed ${new Date().toLocaleTimeString()}` +
    (bad.length ? ` · alert delivery failing: ${bad.map(([n, a]) => `${n} (${a.last_error})`).join("; ")}` : "");
}

async function renderEvents() {
  const r = await fetch("/api/events?limit=25");
  if (!r.ok) return;
  const evs = await r.json();
  document.getElementById("events").replaceChildren(...evs.map((e) => {
    const li = el("li");
    li.append(el("span", "when", fmtTime(e.ts)), `${e.monitor}: ${e.previous} → ${e.current} (${e.message})`);
    return li;
  }));
}

async function renderFindings() {
  const r = await fetch("/api/infra/findings");
  if (!r.ok) return;
  const list = (await r.json()).findings;
  document.getElementById("findings-panel").hidden = list.length === 0;
  document.getElementById("findings").replaceChildren(...list.map((f) => {
    const li = el("li", `finding ${f.severity}`);
    li.append(statusChip(f.severity === "warning" ? "warn" : "pending", f.severity), " ");
    const a = el("a", null, `${f.port_key} on ${f.switch_id}`);
    a.href = `/port?switch_id=${encodeURIComponent(f.switch_id)}&port=${encodeURIComponent(f.port_key)}`;
    li.append(a, ` ${f.message}`);
    return li;
  }));
}

async function refresh() {
  try {
    const r = await fetch("/api/monitors");
    if (r.ok) { lastData = await r.json(); render(lastData); }
    await renderEvents();
    await renderFindings();
  } catch (_) {
    document.getElementById("footer").textContent = "observe unreachable, retrying";
  }
}

function rerender() { if (lastData) render(lastData); }
stateFilter.addEventListener("change", rerender);
search.addEventListener("input", rerender);
function jump(state) { stateFilter.value = state; rerender(); sectionsEl.scrollIntoView(); }
document.getElementById("kpi-down").addEventListener("click", () => jump("down"));
document.getElementById("kpi-warn").addEventListener("click", () => jump("warn"));
document.getElementById("kpi-capacity").addEventListener("click", () => {
  const panel = document.getElementById("capacity-panel");
  if (!panel.hidden) { panel.open = true; panel.scrollIntoView(); }
});
initTiles(() => { if (lastData) render(lastData); });
refresh();
setInterval(refresh, 10000);
