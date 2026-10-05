// Port page: live state, current properties, property history, findings with acknowledge,
// and matched monitors. Every string came from a field report, so it is written with
// textContent only, never as markup.
import { el, statePill, when, api, whoami } from "/static/infra-common.js";

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

function section(title, ...kids) {
  const s = el("section", "sec");
  s.append(el("h2", null, title), ...kids);
  return s;
}

function cell(child) {
  const td = el("td");
  td.append(child);
  return td;
}

function table(heads, rows) {
  const t = el("table", "items");
  const h = el("tr");
  for (const x of heads) h.append(el("th", null, x));
  t.append(h);
  for (const r of rows) {
    const tr = el("tr");
    for (const c of r) tr.append(c instanceof Node ? c : el("td", null, String(c)));
    t.append(tr);
  }
  return t;
}

function findingsBlock(d) {
  if (!d.findings.length) return el("p", "note", "No findings for this port.");
  const ul = el("ul", "findings");
  for (const f of d.findings) {
    const li = el("li", `finding ${f.severity}`);
    li.append(el("span", `pill ${f.severity === "warning" ? "warn" : "pending"}`, f.severity),
      ` ${f.message} `);
    if (f.acknowledged) {
      li.append(el("span", "note", `Acknowledged by ${f.acked_by} on ${when(f.acked_at)}.`));
    } else if (isAdmin) {
      const b = el("button", null, "Acknowledge");
      b.type = "button";
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api("POST", "/api/admin/infra/findings/ack", csrf,
            { switch_id: d.switch_id, port_key: d.port_key, kind: f.kind });
        } catch (e) { document.getElementById("footer").textContent = e.message; }
        await refresh();
      });
      li.append(b);
    }
    ul.append(li);
  }
  return ul;
}

function render(d) {
  const frag = document.createDocumentFragment();
  const head = el("div", "banner");
  head.append(el("strong", null, `${d.port_key} on ${d.switch.name || d.switch_id} `), statePill(d),
    el("div", "note", `role ${d.role}; first seen ${when(d.first_seen)}; last seen ${when(d.last_seen)}` +
      (d.switch.mgmt_addresses.length ? `; switch addresses ${d.switch.mgmt_addresses.join(", ")}` : "")));
  frag.append(head);
  const reported = (v, unit) => (v == null ? "not reported" : `${v}${unit}`);
  frag.append(section("Live state", d.monitors.length
    ? table(["Monitor", "Kind", "State", "Speed", "VLAN", "PoE"], d.monitors.map((m) => [
      m.monitor, m.kind, cell(statePill(m)),
      reported(m.live && m.live.speed_mbps, " Mbit/s"), reported(m.live && m.live.vlan, ""),
      reported(m.live && m.live.poe_w, " W")]))
    : el("p", "note", "No monitor is matched to this port, so there is no live state.")));
  frag.append(section("Findings", findingsBlock(d)));
  const names = Object.keys(d.properties);
  frag.append(section("Current properties", names.length
    ? table(["Property", "Value", "Source", "Observed", "Last verified", "Recorded by"],
      names.map((n) => {
        const p = d.properties[n];
        return [n, fmtValue(p), p.source, when(p.observed_at), when(p.last_verified), p.recorded_by || "unknown"];
      }))
    : el("p", "note", "No field properties have been recorded for this port.")));
  const rows = [];
  for (const n of names) for (const p of d.history[n] || []) rows.push([n, p]);
  rows.sort((a, b) => b[1].observed_at - a[1].observed_at);
  frag.append(section("Property history", rows.length
    ? table(["Property", "Value", "Observed", "Recorded", "Recorded by", "Report", "Source"],
      rows.map(([n, p]) => [n, fmtValue(p), when(p.observed_at), when(p.recorded_at),
        p.recorded_by || "unknown", p.report_id || "none", p.source]))
    : el("p", "note", "No history yet.")));
  page.replaceChildren(frag);
  document.getElementById("footer").textContent = `refreshed ${new Date().toLocaleTimeString()}`;
}

async function refresh() {
  try {
    const q = new URLSearchParams({ switch_id: switchId, port: portName });
    const r = await fetch(`/api/infra/port?${q}`);
    if (r.status === 401) { location.assign("/login"); return; }
    if (r.status === 404) { page.replaceChildren(el("p", null, "Unknown port.")); return; }
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
