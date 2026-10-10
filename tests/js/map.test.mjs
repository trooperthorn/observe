// Run in CI with: node --test tests/js
import test from "node:test";
import assert from "node:assert/strict";
import {
  deviceRows, deviceTypeWord, graphSubLabel, linkRows, stateWord, switchTiers,
} from "../../observe/static/js/map-logic.js";
import { buildGraph } from "../../observe/static/js/graph/infra.js";

// The shape the UniFi feed gives the map (bug plan WP4): each device names the device it is
// uplinked to, drawn between a port "uplink" on the child and a port "to-<child>" on the parent.
function unifiMap(devices) {
  const nodes = [], edges = [];
  for (const d of devices) {
    nodes.push({ id: `switch:${d.id}`, kind: "switch", label: d.label, state: d.state || "up",
      device_type: d.type, monitor: "unifi", address: d.address || "" });
  }
  for (const d of devices.filter((x) => x.up)) {
    nodes.push({ id: `port:${d.id}|uplink`, kind: "port", label: "uplink", parent: `switch:${d.id}`, role: "uplink" });
    nodes.push({ id: `port:${d.up}|to-${d.id}`, kind: "port", label: `to-${d.id}`, parent: `switch:${d.up}`, role: "unknown" });
    edges.push({ id: edges.length + 1, a: `port:${d.id}|uplink`, b: `port:${d.up}|to-${d.id}`,
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

test("an unlinked gateway is still core; unlinked switches stay in access", () => {
  const { nodes, edges } = unifiMap(SEVEN.map((d) => ({ ...d, up: undefined })));
  const tier = switchTiers(nodes, edges);
  assert.equal(edges.length, 0);
  assert.equal(tier.get("switch:gw"), "core");
  assert.equal(tier.get("switch:sw1"), "access");
  assert.equal(tier.get("switch:ap1"), "access");
});

test("switches with no type are placed by their links as before", () => {
  const { nodes, edges } = unifiMap([
    { id: "a", label: "a" }, { id: "b", label: "b", up: "a" }, { id: "c", label: "c", up: "b" },
    { id: "d", label: "lonely" },
  ]);
  const tier = switchTiers(nodes, edges);
  assert.deepEqual(["a", "b", "c", "d"].map((x) => tier.get(`switch:${x}`)),
    ["core", "distribution", "access", "access"]);
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
  assert.deepEqual(rows[0], { from: "uplink on Core switch", to: "to-sw1 on UCG Fiber",
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
