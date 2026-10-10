// Run in CI with: node --test tests/js
import test from "node:test";
import assert from "node:assert/strict";
import {
  deviceRows, deviceTypeWord, graphSubLabel, isPlaceholderPort, linkRows, portNames, portShortName, portText,
  stateWord, switchTiers,
} from "../../observe/static/js/map-logic.js";
import { buildGraph } from "../../observe/static/js/graph/infra.js";

// The shape the UniFi feed gives the map (bug plan WP4): each device names the device it is
// uplinked to, drawn between a port "uplink" on the child and a port "to-<child>" on the parent.
// A device id as the 12 hex digits of a MAC, the way the feed names the parent's port.
const macOf = (id) => [...id].map((c) => c.charCodeAt(0).toString(16)).join("").padStart(12, "0").slice(-12);

function unifiMap(devices) {
  const nodes = [], edges = [];
  for (const d of devices) {
    nodes.push({ id: `switch:${d.id}`, kind: "switch", label: d.label, state: d.state || "up",
      device_type: d.type, monitor: "unifi", address: d.address || "" });
  }
  for (const d of devices.filter((x) => x.up)) {
    nodes.push({ id: `port:${d.id}|uplink`, kind: "port", label: "uplink", parent: `switch:${d.id}`, role: "uplink" });
    nodes.push({ id: `port:${d.up}|to-${macOf(d.id)}`, kind: "port", label: `to-${macOf(d.id)}`, parent: `switch:${d.up}`, role: "unknown" });
    edges.push({ id: edges.length + 1, a: `port:${d.id}|uplink`, b: `port:${d.up}|to-${macOf(d.id)}`,
      source: "config", state: "active", age_days: 0 });
  }
  return { nodes, edges };
}

const SEVEN = [
  { id: "gw", label: "UCG Fiber", type: "gateway", address: "192.0.2.1" },
  { id: "sw1", label: "Core switch", type: "switch", up: "gw" },
  { id: "sw2", label: "Lab switch", type: "switch", up: "sw1" },
  { id: "ap1", label: "AP hall", type: "access_point", up: "sw1" },
  { id: "ap2", label: "AP office", type: "access_point", up: "sw1", state: "down" },
  { id: "ap3", label: "AP lab", type: "access_point", up: "sw2" },
  { id: "ap4", label: "AP garden", type: "access_point", up: "gw" },
];

test("the gateway is core, access points are access, switches go by depth", () => {
  const { nodes, edges } = unifiMap(SEVEN);
  const tier = switchTiers(nodes, edges);
  assert.equal(tier.get("switch:gw"), "core");
  assert.equal(tier.get("switch:sw1"), "distribution");
  assert.equal(tier.get("switch:sw2"), "distribution");
  for (const ap of ["ap1", "ap2", "ap3", "ap4"]) assert.equal(tier.get(`switch:${ap}`), "access", ap);
});

test("an unlinked gateway is still core; devices with no known uplink are unplaced", () => {
  const { nodes, edges } = unifiMap(SEVEN.map((d) => ({ ...d, up: undefined })));
  const tier = switchTiers(nodes, edges);
  assert.equal(edges.length, 0);
  assert.equal(tier.get("switch:gw"), "core");
  assert.equal(tier.get("switch:sw1"), "unplaced");
  assert.equal(tier.get("switch:ap1"), "unplaced");
});

test("a switch with no known uplink is unplaced, not core beside the gateway", () => {
  // The reported map: "Hydro - USW Flex" has no uplink but an access point hangs off it, so it
  // was the top of its own chain and drawn in Core.
  const { nodes, edges } = unifiMap([...SEVEN,
    { id: "hydro", label: "Hydro - USW Flex", type: "switch" },
    { id: "ap5", label: "AP hydro", type: "access_point", up: "hydro" }]);
  const tier = switchTiers(nodes, edges);
  assert.equal(tier.get("switch:hydro"), "unplaced");
  assert.equal(tier.get("switch:ap5"), "access");
  assert.equal(tier.get("switch:gw"), "core");
  assert.equal(tier.get("switch:sw1"), "distribution");
});

test("switches with no type are placed by their links as before", () => {
  const { nodes, edges } = unifiMap([
    { id: "a", label: "a" }, { id: "b", label: "b", up: "a" }, { id: "c", label: "c", up: "b" },
    { id: "d", label: "lonely" },
  ]);
  const tier = switchTiers(nodes, edges);
  // With no gateway the top of the chain is core; a device linked to nothing is unplaced.
  assert.deepEqual(["a", "b", "c", "d"].map((x) => tier.get(`switch:${x}`)),
    ["core", "distribution", "access", "unplaced"]);
  assert.equal(deviceTypeWord(nodes[0]), "Switch");
});

test("device rows list every device by type with its state, monitor and address", () => {
  const { nodes } = unifiMap(SEVEN);
  const rows = deviceRows(nodes);
  assert.equal(rows.length, 7);
  assert.deepEqual(rows.map((r) => r.type), ["Gateway", "Switch", "Switch", "Access point",
    "Access point", "Access point", "Access point"]);
  assert.deepEqual(rows[0], { id: "switch:gw", name: "UCG Fiber", type: "Gateway", state: "up",
    stateText: "Up", monitor: "unifi", address: "192.0.2.1" });
  assert.equal(rows.find((r) => r.name === "AP office").stateText, "Down");
  assert.deepEqual(rows.slice(1, 3).map((r) => r.name), ["Core switch", "Lab switch"]);
});

test("link rows name each port with its device", () => {
  const { nodes, edges } = unifiMap(SEVEN);
  const rows = linkRows(edges, nodes);
  assert.equal(rows.length, 6);
  // The placeholder ports read as words, never as "to-0cea14f15471".
  assert.deepEqual(rows[0], { from: "Core switch uplink", to: "UCG Fiber, port not reported",
    source: "config", seen: "0 days ago", state: "active" });
  assert.equal(linkRows([{ a: "x", b: "y", source: "lldp", age_days: 9, state: "stale" }], [])[0].state,
    "stale, not confirmed recently");
});

test("state words come from the shared vocabulary and graph labels carry them", () => {
  assert.equal(stateWord({ state: "warn" }), "Warning");
  assert.equal(stateWord({ state: "unknown" }), "State unknown");
  assert.equal(stateWord({ state: "unreachable", blocked_by: "core" }), "Unreachable, behind core");
  assert.equal(graphSubLabel({ state: "up", device_type: "gateway" }), "Up · Gateway");
  const g = buildGraph(unifiMap(SEVEN));
  const gw = g.entities.find((e) => e.id === "switch:gw");
  assert.equal(gw.kind, "Gateway");
  assert.equal(gw.sub, "Up · Gateway");
  assert.equal(g.entities.find((e) => e.id === "switch:ap2").sub, "Down · Access point");
});

test("placeholder ports are named in words and show no state; UniFi port keys read Port N", () => {
  // The reported map: ports showed as "to-0cea14f15471" and every Tiers port as "(State unknown)".
  const { nodes, edges } = unifiMap(SEVEN);
  nodes.push({ id: "port:sw1|port5", kind: "port", label: "port5", parent: "switch:sw1", role: "unknown", state: "up" });
  const names = portNames(nodes, edges);
  assert.equal(names.get("port:sw1|uplink"), "uplink to UCG Fiber");
  assert.equal(names.get(`port:gw|to-${macOf("sw1")}`), "link to Core switch");
  assert.equal(names.get("port:sw1|port5"), "Port 5");
  const up = nodes.find((n) => n.id === "port:sw1|uplink");
  assert.equal(isPlaceholderPort(up), true);
  assert.equal(portText(up, names, "State unknown"), "uplink to UCG Fiber");
  const real = nodes.find((n) => n.id === "port:sw1|port5");
  assert.equal(isPlaceholderPort(real), false);
  assert.equal(portText(real, names, "Up"), "Port 5 (Up)");
  // A port with no link and a name of its own keeps it.
  const lone = (label) => portNames([{ id: `port:x|${label}`, kind: "port", label, parent: "switch:x" }], []).get(`port:x|${label}`);
  assert.equal(lone("ge-0/0/1"), "ge-0/0/1");
  assert.equal(lone("uplink"), "uplink");
  assert.equal(lone("to-0cea14f15471"), "link to another device");
  // The short name for "<port> to <device>" lines in the graph's selection panel.
  assert.equal(portShortName({ label: "to-0cea14f15471" }), "unreported port");
  assert.equal(portShortName({ label: "port12" }), "Port 12");
  assert.equal(portShortName({ label: "uplink" }), "uplink");
});
