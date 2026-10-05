// Infrastructure map: core, distribution, access, jacks and endpoints, drawn from
// /api/infra/map. Every node shows its state in words, so colour is never the only signal.
"use strict";

const LAYER_TITLES = [
  ["core", "Core"], ["distribution", "Distribution"], ["access", "Access"],
  ["jack", "Jacks"], ["endpoint", "Endpoints"],
];
const SVG_NS = "http://www.w3.org/2000/svg";
const layersEl = document.getElementById("layers");
const msg = document.getElementById("msg");
const siteSel = document.getElementById("site");
const buildingSel = document.getElementById("building");
let lastData = null;

function switchTiers(nodes, edges) {
  const switches = nodes.filter((n) => n.kind === "switch");
  const ports = new Map(nodes.filter((n) => n.kind === "port").map((n) => [n.id, n]));
  const above = new Map(switches.map((s) => [s.id, new Set()]));
  const linked = new Set();
  for (const e of edges) {
    const a = ports.get(e.a), b = ports.get(e.b);
    if (!a || !b || a.parent === b.parent) continue;
    linked.add(a.parent); linked.add(b.parent);
    // The switch whose port is the uplink sits below the switch at the other end.
    if (a.role === "uplink" && b.role !== "uplink" && above.has(a.parent)) above.get(a.parent).add(b.parent);
    else if (b.role === "uplink" && a.role !== "uplink" && above.has(b.parent)) above.get(b.parent).add(a.parent);
  }
  const level = new Map();
  const depth = (id, seen) => {
    if (level.has(id)) return level.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    let d = 0;
    for (const p of above.get(id) || []) d = Math.max(d, 1 + depth(p, seen));
    seen.delete(id);
    level.set(id, d);
    return d;
  };
  for (const s of switches) depth(s.id, new Set());
  const top = Math.max(0, ...level.values());
  const tier = new Map();
  for (const s of switches) {
    const l = level.get(s.id);
    if (!linked.has(s.id) || top === 0) tier.set(s.id, "access");
    else if (l === 0) tier.set(s.id, "core");
    else if (l === top) tier.set(s.id, "access");
    else tier.set(s.id, "distribution");
  }
  return tier;
}

function box(node, extra) {
  const b = el("div", `node ${node.state || "unknown"}`);
  b.append(el("div", "node-title", node.label), el("div", "node-state", stateText(node)));
  if (extra) b.append(extra);
  return b;
}

function switchBox(node, ports) {
  const list = el("ul", "chips");
  for (const p of ports) {
    const li = el("li");
    const a = el("a", `chip ${p.state}`, `${p.label} (${stateText(p)})`);
    a.href = portHref(p.id.slice(5).split("|")[0], p.label);
    li.append(a);
    if (p.findings && p.findings.length) {
      li.append(el("span", "note", ` ${p.findings.length} finding${p.findings.length === 1 ? "" : "s"}`));
    }
    list.append(li);
  }
  const wrap = el("div");
  wrap.append(el("div", "note", node.monitor ? `monitor ${node.monitor}` : "no monitor linked"), list);
  return box(node, wrap);
}

function draw(data) {
  const nodes = data.nodes, edges = data.edges;
  const tier = switchTiers(nodes, edges);
  const rows = new Map(LAYER_TITLES.map(([k]) => [k, []]));
  const boxOf = new Map();
  for (const n of nodes) {
    if (n.kind === "switch") {
      const ports = nodes.filter((p) => p.kind === "port" && p.parent === n.id);
      const b = switchBox(n, ports);
      boxOf.set(n.id, b);
      for (const p of ports) boxOf.set(p.id, b);
      rows.get(tier.get(n.id)).push(b);
    } else if (n.kind === "jack") {
      const b = box(n, el("div", "note",
        `room ${n.room || "unknown"}, site ${n.site || "unknown"}${n.building ? ", building " + n.building : ""}`));
      boxOf.set(n.id, b);
      rows.get("jack").push(b);
    } else if (n.kind === "endpoint") {
      const b = box(n, el("div", "note", `${n.endpoint_kind}${n.address ? " " + n.address : ""}`));
      boxOf.set(n.id, b);
      rows.get("endpoint").push(b);
    }
  }
  const frag = document.createDocumentFragment();
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "edges");
  svg.setAttribute("aria-hidden", "true");
  frag.append(svg);
  let any = false;
  for (const [key, title] of LAYER_TITLES) {
    if (!rows.get(key).length) continue;
    any = true;
    const sec = el("section", `layer layer-${key}`);
    const row = el("div", "row");
    row.append(...rows.get(key));
    sec.append(el("h2", null, title), row);
    frag.append(sec);
  }
  if (!any) frag.append(el("p", "note", "Nothing is mapped yet for this view."));
  layersEl.replaceChildren(frag);
  drawEdges(svg, edges, boxOf);
  fillLinks(edges, nodes);
}

function drawEdges(svg, edges, boxOf) {
  const base = layersEl.getBoundingClientRect();
  svg.setAttribute("width", String(Math.ceil(base.width)));
  svg.setAttribute("height", String(Math.ceil(base.height)));
  for (const e of edges) {
    const x = boxOf.get(e.a), y = boxOf.get(e.b);
    if (!x || !y || x === y) continue;
    let ra = x.getBoundingClientRect(), rb = y.getBoundingClientRect();
    if (ra.top > rb.top) [ra, rb] = [rb, ra];
    if (Math.abs(ra.top - rb.top) < 2) continue; // same layer; the table lists it
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", String(ra.left + ra.width / 2 - base.left));
    line.setAttribute("y1", String(ra.bottom - base.top));
    line.setAttribute("x2", String(rb.left + rb.width / 2 - base.left));
    line.setAttribute("y2", String(rb.top - base.top));
    line.setAttribute("class", `edge ${e.state}`);
    svg.append(line);
  }
}

function fillLinks(edges, nodes) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const name = (id) => {
    const n = byId.get(id);
    if (!n) return id;
    if (n.kind !== "port") return n.label;
    const sw = byId.get(n.parent);
    return `${n.label} on ${sw ? sw.label : n.parent}`;
  };
  document.querySelector("#links tbody").replaceChildren(...edges.map((e) => {
    const tr = el("tr");
    tr.append(el("td", null, name(e.a)), el("td", null, name(e.b)), el("td", null, e.source),
      el("td", null, `${e.age_days} days ago`),
      el("td", null, e.state === "stale" ? "stale, not confirmed recently" : "active"));
    return tr;
  }));
}

function option(value, text) {
  const o = el("option", null, text);
  o.value = value;
  return o;
}

function fillOptions(all) {
  const jacks = all.nodes.filter((n) => n.kind === "jack");
  const sites = [...new Set(jacks.map((j) => j.site).filter(Boolean))].sort();
  const keepSite = siteSel.value;
  siteSel.replaceChildren(option("", "All sites"), ...sites.map((s) => option(s, s)));
  siteSel.value = sites.includes(keepSite) ? keepSite : "";
  const buildings = [...new Set(jacks.filter((j) => !siteSel.value || j.site === siteSel.value)
    .map((j) => j.building).filter(Boolean))].sort();
  const keepBuilding = buildingSel.value;
  buildingSel.replaceChildren(option("", "All buildings"), ...buildings.map((b) => option(b, b)));
  buildingSel.value = buildings.includes(keepBuilding) ? keepBuilding : "";
}

function summarize(nodes) {
  const counts = {};
  for (const n of nodes.filter((x) => x.kind === "switch")) counts[n.state] = (counts[n.state] || 0) + 1;
  document.getElementById("summary").replaceChildren(
    ...Object.entries(counts).map(([s, c]) => el("span", `pill ${s}`, `${c} ${STATE_WORDS[s] || s}`)));
}

async function refresh() {
  try {
    const all = await api("GET", "/api/infra/map");
    fillOptions(all);
    const site = siteSel.value, building = buildingSel.value;
    let data = all;
    if (site || building) {
      const q = new URLSearchParams();
      if (site) q.set("site", site);
      if (building) q.set("building", building);
      data = await api("GET", `/api/infra/map?${q}`);
    }
    lastData = data;
    msg.textContent = "";
    summarize(data.nodes);
    draw(data);
    document.getElementById("footer").textContent = `refreshed ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    if (e.message !== "not signed in") {
      document.getElementById("footer").textContent = "watchpost unreachable, retrying";
    }
  }
}

siteSel.addEventListener("change", refresh);
buildingSel.addEventListener("change", refresh);
window.addEventListener("resize", () => { if (lastData) draw(lastData); });
refresh();
setInterval(refresh, 15000);
