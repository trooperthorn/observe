// Turns the /api/v2/map payload into the graph engine's input. Pure functions, no DOM.
// Switches are the graph nodes. Endpoints are collapsed into a count on the switch they hang off.
import { FORCE_NODE_LIMIT } from "./force.js";

export const GROUPS = [{ id: "switch", label: "Switch" }];
const STATE_MAP = { unreachable: "unreach", unknown: "pending" };

export function graphState(state) {
  return STATE_MAP[state] || state || "pending";
}

export function linkKind(source) {
  return source === "lldp" || source === "mac" ? source : "uplink";
}

// Entities, relations and anchors for layoutForce, plus a per-switch detail record for the side card.
export function buildGraph(data) {
  const nodes = data.nodes || [];
  const ports = new Map(nodes.filter((n) => n.kind === "port").map((n) => [n.id, n]));
  const switches = nodes.filter((n) => n.kind === "switch");
  const endpoints = new Map();
  const pairs = new Map();
  const details = new Map(switches.map((s) => [s.id, { links: [], endpoints: 0, ports: [] }]));
  for (const p of ports.values()) {
    if (details.has(p.parent)) details.get(p.parent).ports.push(p);
  }
  for (const e of data.edges || []) {
    const a = ports.get(e.a), b = ports.get(e.b);
    const epA = String(e.a).startsWith("endpoint:"), epB = String(e.b).startsWith("endpoint:");
    if (a && epB && details.has(a.parent)) endpoints.set(e.b, a.parent);
    if (b && epA && details.has(b.parent)) endpoints.set(e.a, b.parent);
    if (!a || !b || a.parent === b.parent || !details.has(a.parent) || !details.has(b.parent)) continue;
    const [s, t] = a.parent < b.parent ? [a.parent, b.parent] : [b.parent, a.parent];
    const key = `${s}>${t}`;
    const at = s === a.parent ? a : b, bt = s === a.parent ? b : a;
    const rel = pairs.get(key) || { id: key, source: s, target: t, kind: linkKind(e.source), stale: true, via: [], ownS: [], ownT: [] };
    if (e.state !== "stale") rel.stale = false;
    rel.via.push(`${at.label} to ${bt.label}`);
    rel.ownS.push(at.label);
    rel.ownT.push(bt.label);
    pairs.set(key, rel);
  }
  for (const sw of endpoints.values()) details.get(sw).endpoints += 1;
  const relations = [...pairs.values()];
  const byId = new Map(switches.map((s) => [s.id, s]));
  for (const r of relations) {
    details.get(r.source).links.push({ other: r.target, via: r.via, own: r.ownS, stale: r.stale });
    details.get(r.target).links.push({ other: r.source, via: r.via, own: r.ownT, stale: r.stale });
  }
  const entities = switches.map((s) => {
    const n = details.get(s.id).endpoints;
    return {
      id: s.id, name: s.label, group: "switch", kind: "Switch", state: graphState(s.state),
      stateWord: s.state, badge: n ? `${n} endpoint${n === 1 ? "" : "s"}` : undefined,
      monitor: s.monitor || null,
    };
  });
  const anchors = switches.filter((s) => s.anchor).map((s) => s.id);
  return { entities, relations, anchors, details, byId, limited: entities.length > FORCE_NODE_LIMIT };
}

// Tiers is the default on a phone-sized screen and when the graph is over the force limit.
export function defaultView(nodeCount, narrow) {
  return narrow || nodeCount > FORCE_NODE_LIMIT ? "tiers" : "graph";
}

export function viewFromHash(hash) {
  const v = String(hash || "").replace(/^#/, "");
  return v === "graph" || v === "tiers" || v === "table" ? v : null;
}
