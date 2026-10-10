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

// The UniFi feed's placeholder ports (plugins/unifi/observe_unifi/feed.py): "uplink" on a
// device whose uplink port is not known, and "to-<child mac>" on the device it is uplinked to.
const TO_CHILD = /^to-[0-9a-f]{12}$/;
const UNIFI_PORT = /^port(\d+)$/;

export function isPlaceholderPort(port) {
  const key = String((port && port.label) || "");
  return key === "uplink" || TO_CHILD.test(key);
}

/**
 * Each port's name in words, by port node id: "Port 5" for a UniFi port key (port5), "uplink
 * to <parent>" and "link to <child>" for the placeholder ports, named by the device at the
 * other end of their link, and the port's own label otherwise.
 */
export function portNames(nodes, edges) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const peer = new Map();
  for (const e of edges || []) {
    peer.set(e.a, e.b);
    peer.set(e.b, e.a);
  }
  const deviceAt = (portId) => {
    const p = byId.get(peer.get(portId));
    const sw = p && byId.get(p.kind === "port" ? p.parent : p.id);
    return sw ? sw.label : "";
  };
  const out = new Map();
  for (const n of nodes) {
    if (n.kind !== "port") continue;
    const key = String(n.label || "");
    const num = UNIFI_PORT.exec(key);
    const other = deviceAt(n.id);
    if (num) out.set(n.id, `Port ${num[1]}`);
    else if (key === "uplink") out.set(n.id, other ? `uplink to ${other}` : "uplink");
    else if (TO_CHILD.test(key)) out.set(n.id, other ? `link to ${other}` : "link to another device");
    else out.set(n.id, key);
  }
  return out;
}

// A port's own name, short, for "<port> to <device>" lines: "Port 5", "uplink", or "unreported
// port" for the parent's placeholder.
export function portShortName(port) {
  const key = String((port && port.label) || "");
  const num = UNIFI_PORT.exec(key);
  if (num) return `Port ${num[1]}`;
  return TO_CHILD.test(key) ? "unreported port" : key;
}

// The words beside a port on a Tiers card: its name, and its state in brackets unless it is a
// placeholder port, which has no state of its own to report.
export function portText(port, names, stateText) {
  const name = (names && names.get(port.id)) || port.label;
  return isPlaceholderPort(port) ? name : `${name} (${stateText})`;
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
  const names = portNames(nodes, edges);
  const name = (id) => {
    const n = byId.get(id);
    if (!n) return id;
    if (n.kind !== "port") return n.label;
    const sw = byId.get(n.parent);
    const device = sw ? sw.label : n.parent;
    // A placeholder port is the device's uplink, or a port of the parent that was not reported.
    if (n.label === "uplink") return `${device} uplink`;
    if (isPlaceholderPort(n)) return `${device}, port not reported`;
    return `${names.get(n.id) || n.label} on ${device}`;
  };
  return edges.map((e) => ({
    from: name(e.a), to: name(e.b), source: e.source,
    seen: `${e.age_days} days ago`,
    state: e.state === "stale" ? "stale, not confirmed recently" : "active",
  }));
}
