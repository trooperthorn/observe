// Port page: live state, findings with acknowledge, current properties and property history.
// Every string came from a field report, so it is written with textContent only, never as
// markup.
import { el, stateChip, when, api, whoami } from "/static/infra-common.js";
import { statusChip, monoTag } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { toast } from "/static/js/toast.js";
import { button, card } from "/static/js/admin-ui.js";

const page = document.getElementById("page");
const params = new URLSearchParams(location.search);
const switchId = params.get("switch_id") || "";
const portName = params.get("port") || "";
let csrf = "";
let isAdmin = false;

function fmtValue(p) {
  if (typeof p.value === "boolean") return p.value ? "yes" : "no";
  return `${p.value}${p.unit ? " " + p.unit : ""}`;
}

function table(columns, rows, empty, caption) {
  return sortableTable({ columns, rows, empty, caption }).root;
}

function findingsBlock(d) {
  if (!d.findings.length) return el("p", "muted", "No findings for this port.");
  const ul = el("ul", "finding-list");
  for (const f of d.findings) {
    const li = el("li");
    li.append(statusChip(f.severity === "warning" ? "warn" : "pending", f.severity), el("span", null, f.message));
    if (f.acknowledged) {
      li.append(el("span", "muted", `Acknowledged by ${f.acked_by} on ${when(f.acked_at)}.`));
    } else if (isAdmin) {
      li.append(button("Acknowledge", "", async (ev) => {
        const b = ev.currentTarget;
        b.disabled = true;
        try {
          await api("POST", "/api/v2/findings/ack", csrf,
            { switch_id: d.switch_id, port_key: d.port_key, kind: f.kind });
          toast("Finding acknowledged.", "up");
        } catch (e) { toast(e.message, "down"); }
        await refresh();
      }));
    }
    ul.append(li);
  }
  return ul;
}

function title(d) {
  const t = el("div", "admin-title port-title");
  const crumbs = el("p", "crumbs muted");
  const map = el("a", null, "Network");
  map.href = "/map";
  crumbs.append(map, ` / ${d.switch.name || d.switch_id} / ${d.port_key}`);
  const h = el("h1", null, `${d.port_key} on ${d.switch.name || d.switch_id}`);
  h.append(stateChip(d));
  const sub = el("p", "card-sub", `Role ${d.role}; first seen ${when(d.first_seen)}; last seen ${when(d.last_seen)}` +
    (d.switch.mgmt_addresses.length ? `; switch addresses ${d.switch.mgmt_addresses.join(", ")}` : ""));
  t.append(crumbs, h, sub);
  return t;
}

function render(d) {
  const reported = (v, unit) => (v == null ? "not reported" : `${v}${unit}`);
  const names = Object.keys(d.properties);
  const hist = [];
  for (const n of names) for (const p of d.history[n] || []) hist.push({ name: n, ...p });
  const live = table([
    { key: "mon", label: "Monitor", get: (m) => m.monitor },
    { key: "kind", label: "Kind", get: (m) => m.kind },
    { key: "state", label: "State", get: (m) => m.state, render: (m) => stateChip(m) },
    { key: "speed", label: "Speed", get: (m) => (m.live && m.live.speed_mbps) || 0, render: (m) => reported(m.live && m.live.speed_mbps, " Mbit/s") },
    { key: "vlan", label: "VLAN", get: (m) => (m.live && m.live.vlan) || 0, render: (m) => reported(m.live && m.live.vlan, "") },
    { key: "poe", label: "PoE", get: (m) => (m.live && m.live.poe_w) || 0, render: (m) => reported(m.live && m.live.poe_w, " W") },
  ], d.monitors, "No monitor is matched to this port, so there is no live state.", "Live state");
  const props = table([
    { key: "n", label: "Property", get: (p) => p.name },
    { key: "v", label: "Value", get: (p) => fmtValue(p) },
    { key: "s", label: "Source", get: (p) => p.source, render: (p) => monoTag(p.source) },
    { key: "o", label: "Observed", numeric: true, get: (p) => p.observed_at, render: (p) => when(p.observed_at) },
    { key: "l", label: "Last verified", numeric: true, get: (p) => p.last_verified || 0, render: (p) => when(p.last_verified) },
    { key: "r", label: "Recorded by", get: (p) => p.recorded_by || "unknown" },
  ], names.map((n) => ({ name: n, ...d.properties[n] })), "No field properties have been recorded for this port.", "Current properties");
  const history = table([
    { key: "n", label: "Property", get: (p) => p.name },
    { key: "v", label: "Value", get: (p) => fmtValue(p) },
    { key: "o", label: "Observed", numeric: true, get: (p) => p.observed_at, render: (p) => when(p.observed_at) },
    { key: "rec", label: "Recorded", numeric: true, get: (p) => p.recorded_at, render: (p) => when(p.recorded_at) },
    { key: "by", label: "Recorded by", get: (p) => p.recorded_by || "unknown" },
    { key: "rep", label: "Report", get: (p) => p.report_id || "none" },
    { key: "s", label: "Source", get: (p) => p.source },
  ], hist.sort((a, b) => b.observed_at - a.observed_at), "No history yet.", "Property history");
  page.replaceChildren(title(d),
    card("Live state", null, live),
    card("Findings", null, findingsBlock(d)),
    card("Current properties", null, props),
    card("Property history", null, history));
  document.getElementById("footer").textContent = `refreshed ${new Date().toLocaleTimeString()}`;
}

async function refresh() {
  try {
    const r = await fetch(`/api/v2/ports/${encodeURIComponent(switchId)}/${encodeURIComponent(portName)}`);
    if (r.status === 401) { location.assign("/login"); return; }
    if (r.status === 404) {
      const c = el("section", "card notice");
      c.append(el("h3", null, "Unknown port"), el("p", null, "No port with this switch and name is known."));
      page.replaceChildren(c);
      return;
    }
    if (r.ok) render(await r.json());
  } catch (_) {
    document.getElementById("footer").textContent = "observe unreachable, retrying";
  }
}

(async () => {
  try {
    const me = await whoami();
    csrf = me.csrf;
    isAdmin = !!me.is_admin;
  } catch (_) { return; }
  await refresh();
  setInterval(refresh, 15000);
})();
