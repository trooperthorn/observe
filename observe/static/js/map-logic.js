// What the map page shows about each device, as pure functions with no DOM, so the tiers and
// the table rows can be tested in node. A switch node's `device_type` is what its feed classified
// it as (observe/infra.py DEVICE_TYPES); an empty one, from LLDP, SNMP or a field report, is a
// switch placed by its links alone, as before.
import { stateInfo } from "./chip-states.js";

export const DEVICE_TYPE_WORDS = {
  gateway: "Gateway", switch: "Switch", access_point: "Access point", bridge: "Bridge",
  other: "Other device",
};
// Gateways first, then switches, access points and the rest, for the device table.
const TYPE_ORDER = ["gateway", "switch", "access_point", "bridge", "other"];

export function deviceTypeWord(node) {
  return DEVICE_TYPE_WORDS[node && node.device_type] || "Switch";
}

function typeRank(node) {
  const i = TYPE_ORDER.indexOf(node.device_type || "switch");
  return i < 0 ? TYPE_ORDER.length : i;
}

// The state in words, with the device that holds it back when there is one.
export function stateWord(node) {
  const word = node.state === "unknown" || !node.state ? "State unknown" : stateInfo(node.state).word;
  return node.state === "unreachable" && node.blocked_by ? `${word}, behind ${node.blocked_by}` : word;
}

// The second line of a graph label, for example "Up · Gateway".
export function graphSubLabel(node) {
  return `${stateWord(node)} · ${deviceTypeWord(node)}`;
}

/**
 * The tier of each switch node: core, distribution, access or unplaced. A gateway is always core.
 * A device with an uplink (its port with role uplink links to another device) is placed by link
 * depth below it: the bottom of each chain is access, anything between distribution, and an
 * access point is always access. A device with no known uplink is not a root: it is unplaced,
 * unless the map has no gateway and other devices uplink to it, when it is the top of its
 * chain and so core (a map from LLDP alone).
 */
export function switchTiers(nodes, edges) {
  const switches = nodes.filter((n) => n.kind === "switch");
  const ports = new Map(nodes.filter((n) => n.kind === "port").map((n) => [n.id, n]));
  const above = new Map(switches.map((s) => [s.id, new Set()]));
  for (const e of edges) {
    const a = ports.get(e.a), b = ports.get(e.b);
    if (!a || !b || a.parent === b.parent) continue;
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
  const hasGateway = switches.some((s) => s.device_type === "gateway");
  const below = new Set();
  for (const ups of above.values()) for (const p of ups) below.add(p);
  const tier = new Map();
  for (const s of switches) {
    const l = level.get(s.id);
    const hasUplink = (above.get(s.id) || new Set()).size > 0;
    if (s.device_type === "gateway") tier.set(s.id, "core");
    else if (!hasUplink) tier.set(s.id, !hasGateway && below.has(s.id) ? "core" : "unplaced");
    else if (s.device_type === "access_point" || l === top) tier.set(s.id, "access");
    else tier.set(s.id, "distribution");
  }
  return tier;
}

// One row per device for the table view: gateways first, then switches, access points and the
// rest, each by name.
export function deviceRows(nodes) {
  return nodes.filter((n) => n.kind === "switch")
    .sort((a, b) => typeRank(a) - typeRank(b) || String(a.label).localeCompare(String(b.label)))
    .map((n) => ({
      id: n.id, name: n.label, type: deviceTypeWord(n), state: n.state || "unknown",
      stateText: stateWord(n), monitor: n.monitor || "", address: n.address || "",
    }));
}

// One row per link for the table view. A port is named with the device it is on.
export function linkRows(edges, nodes) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const name = (id) => {
    const n = byId.get(id);
    if (!n) return id;
    if (n.kind !== "port") return n.label;
    const sw = byId.get(n.parent);
    return `${n.label} on ${sw ? sw.label : n.parent}`;
  };
  return edges.map((e) => ({
    from: name(e.a), to: name(e.b), source: e.source,
    seen: `${e.age_days} days ago`,
    state: e.state === "stale" ? "stale, not confirmed recently" : "active",
  }));
}
