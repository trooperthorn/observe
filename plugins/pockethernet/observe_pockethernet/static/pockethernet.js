// Field report list, report detail and jack pages. Every string came from a phone, so it is
// written with textContent only, never as markup. Helpers (el, when, whoami, portHref) come
// from the /static/infra-common.js module, which this file loads as a module script.
import { el, when, whoami, portHref } from "/static/infra-common.js";
import { statusChip, monoTag } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { svg } from "/static/js/dom.js";

const page = document.getElementById("page");
const footer = document.getElementById("footer");
const params = new URLSearchParams(location.search);
const API = "/api/plugins/pockethernet";
const PAGE_SIZE = 50;

// A titled card from the shared components. Titles and values are set with textContent.
function section(title, ...kids) {
  const s = el("section", "card");
  s.append(el("h3", null, title), ...kids);
  return s;
}

function crumbs(...parts) {
  const p = el("p", "crumbs");
  parts.forEach((x, i) => { if (i) p.append(" / "); p.append(x); });
  return p;
}

// Pass, Fail and Warn chips: an icon and a word, never colour alone.
const VERDICTS = { pass: ["ok", "Pass"], ok: ["ok", "Pass"], fail: ["down", "Fail"],
  failed: ["down", "Fail"], error: ["down", "Fail"], warn: ["warn", "Warn"], warning: ["warn", "Warn"] };
function verdictChip(v) {
  const m = VERDICTS[String(v || "").toLowerCase()];
  return m ? statusChip(m[0], m[1]) : statusChip("unavailable", v ? String(v) : "No result");
}

function kpi(value, label) {
  const k = el("div", "kpi");
  k.append(el("span", "kpi-value", String(value)), el("span", "kpi-label", label));
  return k;
}

// The sortable shared table. Column render functions return Nodes.
function sortable(columns, rows, empty, caption) {
  return sortableTable({ columns, rows, empty, caption }).root;
}

function cell(...kids) {
  const s = el("span");
  s.append(...kids);
  return s;
}

function table(heads, rows) {
  const columns = heads.map((h, i) => ({ key: String(i), label: h,
    render: (r) => {
      const c = r[i];
      if (c instanceof Node) return c;
      return c === null || c === undefined ? "" : String(c);
    } }));
  return sortable(columns, rows, "Nothing to show.", "");
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
  const td = el("span");
  if (!ports.length) { td.textContent = "none derived"; return td; }
  ports.forEach((p, i) => {
    if (i) td.append(", ");
    td.append(link(`${p.port_key} on ${p.switch_id}`, portHref(p.switch_id, p.port_key)));
  });
  return td;
}

function jackCell(key) {
  return key ? cell(link(key, jackHref(key))) : cell();
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

const PASSING = new Set(["pass", "ok"]);

function failCount(r) {
  return VERDICTS[String(r.verdict || "").toLowerCase()]?.[0] === "down" ? 1 : 0;
}

async function listView() {
  const offset = Math.max(0, parseInt(params.get("offset") || "0", 10) || 0);
  const d = await get("/reports", { limit: PAGE_SIZE, offset });
  const frag = document.createDocumentFragment();
  frag.append(crumbs("Network", "Cable reports"), el("h1", null, "Field reports"));
  if (!d.reports.length) {
    frag.append(section("Reports", el("p", "empty muted", "No field reports have been uploaded yet.")));
    page.replaceChildren(frag);
    return;
  }
  const columns = [
    { key: "taken", label: "Date", get: (r) => r.taken_at,
      render: (r) => cell(link(when(r.taken_at), reportHref(r)),
        ...(r.clock_corrected ? [" ", monoTag("clock corrected")] : [])) },
    { key: "site", label: "Site", get: (r) => r.site || "", render: (r) => r.site || "" },
    { key: "jacks", label: "Jacks", numeric: true, get: (r) => (r.port_id ? 1 : 0),
      render: (r) => (r.port_id ? jackCell(r.port_id) : "0") },
    { key: "fails", label: "Fails", get: (r) => failCount(r),
      render: (r) => (failCount(r) ? verdictChip("fail")
        : PASSING.has(String(r.verdict || "").toLowerCase()) ? verdictChip("pass")
        : verdictChip(r.verdict ? r.verdict : null)) },
    { key: "by", label: "Uploaded by", get: (r) => r.source, render: (r) => r.source },
    { key: "ports", label: "Ports", render: (r) => portCell(r.ports) },
    { key: "state", label: "Status", get: (r) => r.status,
      render: (r) => r.status + (r.body_pruned ? ", body removed by retention" : "") },
  ];
  const search = el("input");
  search.type = "search";
  search.placeholder = "Filter by site, jack or uploader";
  search.setAttribute("aria-label", "Filter reports");
  const box = el("div", "search-box");
  box.append(search);
  const holder = el("div");
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const rows = d.reports.filter((r) => !q ||
      [r.site, r.port_id, r.source, r.report_id].some((x) => String(x || "").toLowerCase().includes(q)));
    holder.replaceChildren(sortable(columns, rows, "No report matches that filter.", "Field reports"));
  };
  search.addEventListener("input", draw);
  draw();
  const nav = el("p", "card-sub", `Showing ${offset + 1} to ${offset + d.reports.length} of ${d.total}. `);
  if (offset > 0) nav.append(link("Newer", `?offset=${Math.max(0, offset - PAGE_SIZE)}`), " ");
  if (offset + d.reports.length < d.total) nav.append(link("Older", `?offset=${offset + PAGE_SIZE}`));
  frag.append(section("Reports", box, holder, nav));
  page.replaceChildren(frag);
}

// The wiremap as SVG. Four pairs, each drawn straight between the same pins at both ends and
// labelled by pair number and colour name. The state of a pair is told by line style as well as
// colour (solid, dotted or dashed) and by the word in its label. Colour names follow T568B.
const PAIRS = [
  { pair: "1-2", pins: [1, 2], colour: "orange", prop: "pair_1_2_length_m" },
  { pair: "3-6", pins: [3, 6], colour: "green", prop: "pair_3_6_length_m" },
  { pair: "4-5", pins: [4, 5], colour: "blue", prop: "pair_4_5_length_m" },
  { pair: "7-8", pins: [7, 8], colour: "brown", prop: "pair_7_8_length_m" },
];

function pairState(p, props) {
  const fault = String(props.pair_fault || "").toLowerCase();
  if (fault && fault !== "none" && fault.includes(p.pair)) return ["down", "fault"];
  if (props[p.prop] !== undefined && props[p.prop] !== null) return ["up", "ok"];
  return ["pending", "no data"];
}

function wiremap(props) {
  const W = 520, top = 28, gap = 26;
  const root = svg("svg", { viewBox: `0 0 ${W} ${top + gap * 8}`, class: "wiremap", role: "img",
    "aria-label": "Wiremap, pins 1 to 8 at both ends" });
  const y = (pin) => top + (pin - 1) * gap + 10;
  root.append(svg("text", { x: 20, y: 14, class: "end" }, "Near end"),
    svg("text", { x: W - 20, y: 14, class: "end", "text-anchor": "end" }, "Far end"));
  for (let pin = 1; pin <= 8; pin++) {
    root.append(svg("circle", { cx: 120, cy: y(pin), r: 5, class: "pin" }),
      svg("circle", { cx: W - 120, cy: y(pin), r: 5, class: "pin" }),
      svg("text", { x: 104, y: y(pin) + 4, "text-anchor": "end", class: "end" }, String(pin)),
      svg("text", { x: W - 104, y: y(pin) + 4, class: "end" }, String(pin)));
  }
  for (const p of PAIRS) {
    const [role, word] = pairState(p, props);
    for (const pin of p.pins) {
      root.append(svg("line", { x1: 125, y1: y(pin), x2: W - 125, y2: y(pin), class: `wire s-${role}` }));
    }
    const mid = (y(p.pins[0]) + y(p.pins[1])) / 2;
    const len = props[p.prop];
    root.append(svg("text", { x: W / 2, y: mid + 4, "text-anchor": "middle" },
      `Pair ${p.pair} ${p.colour}: ${word}${len !== undefined && len !== null ? `, ${len} m` : ""}`));
  }
  return root;
}

function pairTable(props) {
  return table(["Pair", "Colour", "Length", "State"], PAIRS.map((p) => {
    const [role, word] = pairState(p, props);
    const len = props[p.prop];
    return [p.pair, p.colour, len === undefined || len === null ? "not measured" : `${len} m`,
      statusChip(role === "up" ? "ok" : role === "down" ? "down" : "unavailable",
        role === "up" ? "Pass" : role === "down" ? "Fail" : "No data")];
  }));
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

// One report covers one jack, so the row of counts is for that jack and its steps.
function reportKpis(b) {
  const steps = b.steps || [];
  const verdict = VERDICTS[String((b.properties || {}).cable_verdict || "").toLowerCase()];
  const warn = steps.filter((s) => s.status === "warn").length;
  const row = el("div", "kpi-row");
  row.append(kpi(1, "Jacks tested"), kpi(verdict && verdict[0] === "ok" ? 1 : 0, "Pass"),
    kpi(verdict && verdict[0] === "down" ? 1 : 0, "Fail"), kpi(warn, "Warn (steps)"));
  return row;
}

async function reportView() {
  const d = await get("/report", { source: params.get("source") || "", report_id: params.get("report_id") || "" });
  if (!d) { page.replaceChildren(el("p", null, "Unknown report.")); return; }
  const b = d.body;
  const frag = document.createDocumentFragment();
  const head = el("div", "admin-title");
  head.append(crumbs(link("Cable reports", "/plugins/pockethernet"), `Report ${d.report_id}`),
    el("h1", null, `Report ${d.report_id}`),
    el("p", "card-sub", `From ${d.source}; revision ${d.revision} (${d.revisions_seen} received); taken ${when(d.taken_at)}` +
      (d.clock_corrected ? `, phone clock corrected from ${when(d.reported_taken_at_ms / 1000)}` : "") +
      `; received ${when(d.received_at)}; status ${d.status}.`));
  frag.append(head);
  if (b) frag.append(reportKpis(b));
  frag.append(section("Ports", d.ports.length
    ? table(["Port", "Switch"], d.ports.map((p) => [cell(link(p.port_key, portHref(p.switch_id, p.port_key))), p.switch_id]))
    : el("p", "note", "No port properties came from this report.")));
  if (!b) {
    frag.append(section("Report body", el("p", "note",
      "The body was removed by the evidence retention setting. The summary above is kept.")));
    page.replaceChildren(frag);
    return;
  }
  const props = b.properties || {};
  frag.append(section("Jack", table(["Jack", "Result", "Pairs", "Link"], [[
    jackCell((b.site || {}).port_id || b.location_label || ""), verdictChip(props.cable_verdict),
    PAIRS.map((p) => `${p.pair} ${pairState(p, props)[1]}`).join(", "),
    props.link_speed_mbps ? `${props.link_speed_mbps} Mb/s ${props.duplex || ""}`.trim() : "no link data"]])));
  frag.append(section("Wiremap", wiremap(props), pairTable(props)));
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
  const head = el("div", "admin-title");
  head.append(crumbs(link("Cable reports", "/plugins/pockethernet"), `Jack ${d.jack_key}`),
    el("h1", null, `Jack ${d.jack_key}`),
    el("p", "card-sub", `Room ${d.room || "unknown"}, site ${d.site || "unknown"}; first seen ${when(d.first_seen)}; last seen ${when(d.last_seen)}.`));
  frag.append(head);
  const now = el("p");
  if (d.switch_id) now.append(link(`${d.port_key} on ${d.switch_id}`, portHref(d.switch_id, d.port_key)));
  else now.textContent = "Not patched to a port at the moment.";
  frag.append(section("Patched now", now));
  const newest = d.reports[0];
  const latest = newest ? await get("/report", { source: newest.source, report_id: newest.report_id }) : null;
  const props = latest && latest.body ? latest.body.properties || {} : null;
  if (props) {
    frag.append(section("Latest cable test", el("p", "card-sub", `Taken ${when(latest.taken_at)}.`),
      verdictChip(props.cable_verdict), wiremap(props), pairTable(props)));
  } else {
    frag.append(section("Latest cable test", el("p", "note", "No report body is available for this jack.")));
  }
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
