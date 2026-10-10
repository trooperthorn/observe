// Infrastructure map: core, distribution, access, jacks and endpoints, drawn from
// /api/v2/map. Every node shows its state in words, so colour is never the only signal, and its
// device type (gateway, switch, access point), which also decides its tier.
import { el, stateText, portHref, api, poller, STATE_WORDS } from "/static/infra-common.js";
import { svg as svgEl } from "/static/js/dom.js";
import { statusChip } from "/static/js/chips.js";
import { deviceRows, deviceTypeWord, linkRows, switchTiers } from "/static/js/map-logic.js";
import "/static/js/theme.js";
import { layoutForce } from "/static/js/graph/force.js";
import { createGraphView, structureKey, mergeLayout } from "/static/js/graph/view.js";
import { GROUPS, buildGraph, defaultView, viewFromHash } from "/static/js/graph/infra.js";
import { refreshedText } from "/static/js/format.js";

const LAYER_TITLES = [
  ["core", "Core"], ["distribution", "Distribution"], ["access", "Access"],
  ["jack", "Jacks"], ["endpoint", "Endpoints"],
];
const layersEl = document.getElementById("layers");
const msg = document.getElementById("msg");
const siteSel = document.getElementById("site");
const buildingSel = document.getElementById("building");
let lastData = null;
let view = null;
let graphView = null;
let graph = null;
let layout = null;
let layoutKey = "";
const graphEl = document.getElementById("graphview");
const selectedEl = document.getElementById("selected");
const narrow = () => window.matchMedia("(max-width: 600px)").matches;

function currentView(nodeCount) {
  return viewFromHash(location.hash) || view || defaultView(nodeCount, narrow());
}

function clearButton() {
  const b = el("button", "btn", "Clear selection");
  b.type = "button";
  b.addEventListener("click", () => {
    if (graphView) graphView.select(null);
    document.getElementById("graphcanvas").focus();
  });
  return b;
}

function showSelected(id) {
  const h2 = el("h2", null, "Selected");
  if (!graph || !id || !graph.byId.has(id)) {
    selectedEl.replaceChildren(h2, el("p", "note", "Select a device to see its links."));
    return;
  }
  const sw = graph.byId.get(id), d = graph.details.get(id);
  const sid = id.slice("switch:".length);
  const list = el("ul", "chips");
  for (const l of d.links) {
    const other = graph.byId.get(l.other);
    const li = el("li");
    const a = el("a", `chip ${l.stale ? "pending" : "up"}`,
      `${l.own[0]} to ${other ? other.label : l.other}${l.stale ? " (stale)" : ""}`);
    a.href = portHref(sid, l.own[0]);
    li.append(a);
    list.append(li);
  }
  selectedEl.replaceChildren(h2, el("div", "node-title", sw.label),
    el("div", "node-state", stateText(sw)),
    el("div", "note", `${deviceTypeWord(sw)}, ${d.ports.length} mapped port${d.ports.length === 1 ? "" : "s"}, ${d.endpoints} endpoint${d.endpoints === 1 ? "" : "s"}`),
    el("div", "note", sw.monitor ? `monitor ${sw.monitor}` : "no monitor linked"),
    d.links.length ? list : el("p", "note", "No device links mapped."), clearButton());
}

function drawGraph(data) {
  graph = buildGraph(data);
  const note = document.getElementById("graphnote");
  if (!graphView) {
    graphView = createGraphView(document.getElementById("graphcanvas"), {
      onSelect: showSelected,
      onOpen: (id) => {
        const d = graph && graph.details.get(id);
        if (d && d.links.length) location.href = portHref(id.slice("switch:".length), d.links[0].own[0]);
      },
    });
  }
  const box = graphView.state;
  // The 15 second refresh only changes states when the map is the same shape, so the old layout is
  // kept with the new states and the person's pan and zoom stay where they were.
  const key = structureKey(graph.entities, graph.relations, graph.anchors);
  const same = layout !== null && key === layoutKey;
  layout = same ? mergeLayout(layout, graph.entities, graph.relations)
    : layoutForce({ entities: graph.entities, relations: graph.relations, groups: GROUPS,
      anchors: graph.anchors, width: Math.max(box.w, 600), height: Math.max(box.h, 400) });
  layoutKey = key;
  graphView.setData(layout, graph.entities, { sameStructure: same });
  note.hidden = !layout.limited;
  note.textContent = layout.limited
    ? "The graph is limited to 300 devices. Use the tiers or the table for this view." : "";
  showSelected(graphView.state.selected);
}

function applyView(data) {
  const count = data.nodes.filter((n) => n.kind === "switch").length;
  const v = currentView(count);
  const forced = v === "graph" && count > 300 ? "tiers" : v;
  for (const b of document.querySelectorAll(".viewbtn")) {
    b.setAttribute("aria-pressed", String(b.dataset.view === forced));
  }
  graphEl.hidden = forced !== "graph";
  layersEl.hidden = forced !== "tiers";
  // The table view lists the devices, then the links; the links stay under the other views too.
  document.getElementById("devicestable").hidden = forced !== "table";
  document.getElementById("linkstable").hidden = false;
  if (forced === "graph") drawGraph(data);
  else if (forced === "tiers") draw(data);
}

for (const b of document.querySelectorAll(".viewbtn")) {
  b.addEventListener("click", () => {
    view = b.dataset.view;
    try { history.replaceState(null, "", `#${view}`); } catch (e) { location.hash = view; }
    if (lastData) applyView(lastData);
  });
}
window.addEventListener("hashchange", () => { if (lastData) applyView(lastData); });
document.getElementById("zoomin").addEventListener("click", () => graphView && graphView.zoomBy(1.25));
document.getElementById("zoomout").addEventListener("click", () => graphView && graphView.zoomBy(0.8));
document.getElementById("zoomfit").addEventListener("click", () => graphView && graphView.fit());
// Escape clears the selection wherever the focus is, not only on the canvas.
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && graphView && graphView.state.selected && !graphEl.hidden) graphView.select(null);
});

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
  wrap.append(el("div", "note",
    `${deviceTypeWord(node)}, ${node.monitor ? `monitor ${node.monitor}` : "no monitor linked"}`), list);
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
  const svg = svgEl("svg", { class: "edges", "aria-hidden": "true" });
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
    const line = svgEl("line", {
      x1: ra.left + ra.width / 2 - base.left, y1: ra.bottom - base.top,
      x2: rb.left + rb.width / 2 - base.left, y2: rb.top - base.top,
      class: `edge ${e.state}`,
    });
    svg.append(line);
  }
}

function fillLinks(edges, nodes) {
  document.querySelector("#links tbody").replaceChildren(...linkRows(edges, nodes).map((r) => {
    const tr = el("tr");
    tr.append(el("td", null, r.from), el("td", null, r.to), el("td", null, r.source),
      el("td", null, r.seen), el("td", null, r.state));
    return tr;
  }));
}

function fillDevices(nodes) {
  const rows = deviceRows(nodes);
  const body = document.querySelector("#devices tbody");
  if (!rows.length) {
    const td = el("td", "muted", "No devices are mapped yet.");
    td.colSpan = 5;
    const tr = el("tr");
    tr.append(td);
    body.replaceChildren(tr);
    return;
  }
  body.replaceChildren(...rows.map((r) => {
    const tr = el("tr");
    const state = el("td");
    state.append(statusChip(r.state, r.stateText));
    tr.append(el("td", null, r.name), el("td", null, r.type), state,
      el("td", null, r.monitor || "none linked"), el("td", null, r.address || "unknown"));
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
  document.getElementById("map-summary").replaceChildren(
    ...Object.entries(counts).map(([s, c]) => el("span", `pill ${s}`, `${c} ${STATE_WORDS[s] || s}`)));
}

async function refresh() {
  try {
    const all = await api("GET", "/api/v2/map");
    fillOptions(all);
    const site = siteSel.value, building = buildingSel.value;
    let data = all;
    if (site || building) {
      const q = new URLSearchParams();
      if (site) q.set("site", site);
      if (building) q.set("building", building);
      data = await api("GET", `/api/v2/map?${q}`);
    }
    lastData = data;
    msg.textContent = "";
    summarize(data.nodes);
    fillDevices(data.nodes);
    fillLinks(data.edges, data.nodes);
    applyView(data);
    document.getElementById("footer").textContent = refreshedText();
  } catch (e) {
    if (e.message === "not signed in") return;
    document.getElementById("footer").textContent = "observe unreachable, retrying";
    throw e;
  }
}

// A change of filter reads at once; a failed read is left to the poller to retry.
const refreshNow = () => refresh().catch(() => {});
siteSel.addEventListener("change", refreshNow);
buildingSel.addEventListener("change", refreshNow);
window.addEventListener("resize", () => { if (lastData && !layersEl.hidden) draw(lastData); });
poller(refresh, { interval: 15000, domains: ["map", "monitors"] });
