// Field report list, report detail and jack pages. Every string came from a phone, so it is
// written with textContent only, never as markup. Helpers (el, when, whoami, portHref) come
// from the /static/infra-common.js module, which this file loads as a module script.
import { el, when, whoami, portHref } from "/static/infra-common.js";

const page = document.getElementById("page");
const footer = document.getElementById("footer");
const params = new URLSearchParams(location.search);
const API = "/api/plugins/pockethernet";
const PAGE_SIZE = 50;

function section(title, ...kids) {
  const s = el("section", "sec");
  s.append(el("h2", null, title), ...kids);
  return s;
}

function cell(...kids) {
  const td = el("td");
  td.append(...kids);
  return td;
}

function table(heads, rows) {
  const t = el("table", "items");
  const h = el("tr");
  for (const x of heads) h.append(el("th", null, x));
  t.append(h);
  for (const r of rows) {
    const tr = el("tr");
    for (const c of r) {
      tr.append(c instanceof Node ? c : el("td", null, c === null || c === undefined ? "" : String(c)));
    }
    t.append(tr);
  }
  return t;
}

function link(text, href) {
  const a = el("a", null, text);
  a.href = href;
  return a;
}

function reportHref(r) {
  return `/plugins/pockethernet/report?source=${encodeURIComponent(r.source)}&report_id=${encodeURIComponent(r.report_id)}`;
}

function jackHref(key) {
  return `/plugins/pockethernet/jack?key=${encodeURIComponent(key)}`;
}

function portCell(ports) {
  const td = el("td");
  if (!ports.length) { td.textContent = "none derived"; return td; }
  ports.forEach((p, i) => {
    if (i) td.append(", ");
    td.append(link(`${p.port_key} on ${p.switch_id}`, portHref(p.switch_id, p.port_key)));
  });
  return td;
}

function jackCell(key) {
  return key ? cell(link(key, jackHref(key))) : el("td", null, "");
}

function kv(obj) {
  const rows = Object.entries(obj || {})
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .map(([k, v]) => [k, typeof v === "boolean" ? (v ? "yes" : "no") : String(v)]);
  return rows.length ? table(["Field", "Value"], rows) : el("p", "note", "Nothing reported.");
}

async function get(path, query) {
  const r = await fetch(`${API}${path}?${new URLSearchParams(query)}`);
  if (r.status === 401) { location.assign("/login"); throw new Error("not signed in"); }
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`request failed (${r.status})`);
  return r.json();
}

async function listView() {
  const offset = Math.max(0, parseInt(params.get("offset") || "0", 10) || 0);
  const d = await get("/reports", { limit: PAGE_SIZE, offset });
  const frag = document.createDocumentFragment();
  frag.append(el("h2", null, "Field reports"));
  if (!d.reports.length) {
    frag.append(el("p", "note", "No field reports have been uploaded yet."));
  } else {
    frag.append(table(
      ["Taken", "Source", "Jack", "Status", "Revision", "Tester", "Ports", "Report"],
      d.reports.map((r) => [
        when(r.taken_at) + (r.clock_corrected ? " (clock corrected)" : ""), r.source,
        jackCell(r.port_id), r.status + (r.body_pruned ? ", body removed by retention" : ""),
        r.revision, r.tester_serial, portCell(r.ports), cell(link("Open", reportHref(r)))])));
    const nav = el("p", "note", `Showing ${offset + 1} to ${offset + d.reports.length} of ${d.total}. `);
    if (offset > 0) nav.append(link("Newer", `?offset=${Math.max(0, offset - PAGE_SIZE)}`), " ");
    if (offset + d.reports.length < d.total) nav.append(link("Older", `?offset=${offset + PAGE_SIZE}`));
    frag.append(nav);
  }
  page.replaceChildren(frag);
}

function fieldTable(fields) {
  return table(["Field", "Value", "Unit"], fields.map((f) => [f.name, f.value, f.unit || ""]));
}

function stepsBlock(steps) {
  if (!steps.length) return el("p", "note", "No steps were recorded.");
  const frag = document.createDocumentFragment();
  for (const s of steps) {
    frag.append(el("h3", null, `${s.label || s.step} (${s.step}): ${s.status}${s.error ? ", " + s.error : ""}`));
    if (s.fields && s.fields.length) frag.append(fieldTable(s.fields));
  }
  return frag;
}

function toolsBlock(tools) {
  if (!tools.length) return el("p", "note", "No tool results were recorded.");
  const frag = document.createDocumentFragment();
  for (const t of tools) {
    frag.append(el("h3", null, `${t.title || t.tool}${t.target ? " " + t.target : ""}: ${t.verdict}`));
    if (t.headline) frag.append(el("p", null, t.headline));
    if (t.fields && t.fields.length) frag.append(fieldTable(t.fields));
    if (t.details && t.details.length) {
      const ul = el("ul");
      for (const x of t.details) ul.append(el("li", null, x));
      frag.append(ul);
    }
  }
  return frag;
}

function neighborsBlock(list) {
  if (!list.length) return el("p", "note", "No LLDP or CDP neighbour was seen.");
  return table(["Protocol", "System", "Device id", "Port", "Addresses", "VLAN", "Voice VLAN", "Vendor"],
    list.map((n) => [n.protocol, n.system_name || "", n.device_id || "", n.port_id || "",
      (n.management_addresses || []).join(", "), n.vlan_id, n.voice_vlan_id, n.vendor || ""]));
}

async function reportView() {
  const d = await get("/report", { source: params.get("source") || "", report_id: params.get("report_id") || "" });
  if (!d) { page.replaceChildren(el("p", null, "Unknown report.")); return; }
  const b = d.body;
  const frag = document.createDocumentFragment();
  const head = el("div", "banner");
  head.append(el("strong", null, `Report ${d.report_id}`),
    el("div", "note", `From ${d.source}; revision ${d.revision} (${d.revisions_seen} received); taken ${when(d.taken_at)}` +
      (d.clock_corrected ? `, phone clock corrected from ${when(d.reported_taken_at_ms / 1000)}` : "") +
      `; received ${when(d.received_at)}; status ${d.status}.`));
  frag.append(head);
  frag.append(section("Ports", d.ports.length
    ? table(["Port", "Switch"], d.ports.map((p) => [cell(link(p.port_key, portHref(p.switch_id, p.port_key))), p.switch_id]))
    : el("p", "note", "No port properties came from this report.")));
  if (!b) {
    frag.append(section("Report body", el("p", "note",
      "The body was removed by the evidence retention setting. The summary above is kept.")));
    page.replaceChildren(frag);
    return;
  }
  const site = b.site || {};
  frag.append(section("Where", table(["Field", "Value"], [
    ["Jack", jackCell(site.port_id || b.location_label || "")], ["Site", site.site || ""],
    ["Building", site.building || ""], ["Room", site.room || ""], ["Panel", site.panel || ""],
    ["Preset", b.preset_name || ""], ["Notes", b.notes || ""]])));
  frag.append(section("Verdict and properties", kv(b.properties)));
  frag.append(section("Link", kv(b.link)));
  frag.append(section("PoE", kv(b.poe)));
  frag.append(section("DHCP", kv(b.dhcp)));
  frag.append(section("Neighbours", neighborsBlock(b.neighbors || [])));
  frag.append(section("Tester", kv(b.device)));
  if ((b.warnings || []).length) {
    const ul = el("ul");
    for (const w of b.warnings) ul.append(el("li", null, w));
    frag.append(section("Warnings", ul));
  }
  if (b.geo) frag.append(section("Location", kv(b.geo)));
  if (b.wifi) frag.append(section("Wi-Fi", kv(b.wifi)));
  frag.append(section("Steps", stepsBlock(b.steps || [])));
  frag.append(section("Tool results", toolsBlock(b.tool_results || [])));
  page.replaceChildren(frag);
}

async function jackView() {
  const d = await get("/jack", { key: params.get("key") || "" });
  if (!d) { page.replaceChildren(el("p", null, "Unknown jack.")); return; }
  const frag = document.createDocumentFragment();
  const head = el("div", "banner");
  head.append(el("strong", null, `Jack ${d.jack_key}`),
    el("div", "note", `Room ${d.room || "unknown"}, site ${d.site || "unknown"}; first seen ${when(d.first_seen)}; last seen ${when(d.last_seen)}.`));
  frag.append(head);
  const now = el("p");
  if (d.switch_id) now.append(link(`${d.port_key} on ${d.switch_id}`, portHref(d.switch_id, d.port_key)));
  else now.textContent = "Not patched to a port at the moment.";
  frag.append(section("Patched now", now));
  frag.append(section("Patch history", d.history.length
    ? table(["Observed", "Port", "Recorded by", "Report"], d.history.map((h) => [
      when(h.observed_at), cell(link(`${h.port_key} on ${h.switch_id}`, portHref(h.switch_id, h.port_key))),
      h.recorded_by || "unknown", h.report_id || "none"]))
    : el("p", "note", "No patch history has been recorded.")));
  frag.append(section("Links", d.links.length
    ? table(["Port", "Source", "Confidence", "Last seen", "State"], d.links.map((l) => [
      l.port, l.source, l.confidence, when(l.last_seen), l.closed_at ? `closed ${when(l.closed_at)}` : "open"]))
    : el("p", "note", "No links.")));
  frag.append(section("Reports", d.reports.length
    ? table(["Taken", "Source", "Status", "Report"], d.reports.map((r) => [
      when(r.taken_at), r.source, r.status, cell(link(r.report_id, reportHref(r)))]))
    : el("p", "note", "No reports for this jack.")));
  page.replaceChildren(frag);
}

(async () => {
  try {
    await whoami();
    const view = page.dataset.view;
    await (view === "report" ? reportView : view === "jack" ? jackView : listView)();
    footer.textContent = `refreshed ${new Date().toLocaleTimeString()}`;
  } catch (_) {
    if (footer.textContent === "") footer.textContent = "observe unreachable";
  }
})();
