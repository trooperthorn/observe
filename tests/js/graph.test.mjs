// Run in CI with: node --test tests/js
// Node is not needed locally; tests/test_ui_graph.py checks the sources and mirrors the numbers.
import test from "node:test";
import assert from "node:assert/strict";
import {
  layoutForce, tickCount, nodeRadius, FORCE_NODE_LIMIT,
} from "../../observe/static/js/graph/force.js";
import {
  fitCamera, pickNode, worldToScreen, screenToWorld, paletteColor, readTheme,
} from "../../observe/static/js/graph/render.js";
import {
  graphSummary, stepSelection, zoomAt, focusSet,
} from "../../observe/static/js/graph/view.js";

function sample(n) {
  const entities = [];
  const relations = [];
  for (let i = 0; i < n; i++) {
    entities.push({ id: "n" + i, name: "node " + i, group: i % 3 === 0 ? "switch" : "host", state: "up" });
    if (i > 0) relations.push({ id: "l" + i, source: "n" + Math.floor((i - 1) / 3), target: "n" + i, kind: "uplink" });
  }
  return {
    entities, relations, groups: [{ id: "switch" }, { id: "host" }],
    width: 800, height: 600, sizeScale: 1, anchors: ["n0"],
  };
}

test("the same input gives the same positions", () => {
  const a = layoutForce(sample(40));
  const b = layoutForce(sample(40));
  assert.deepEqual(a, b);
  assert.equal(a.nodes.length, 40);
  assert.equal(a.links.length, 39);
  for (const n of a.nodes) assert.ok(Number.isFinite(n.x) && Number.isFinite(n.y));
});

test("the anchor settles near the centre", () => {
  const out = layoutForce(sample(30));
  const anchor = out.nodes.find((n) => n.entityId === "n0");
  assert.ok(Math.hypot(anchor.x - 400, anchor.y - 300) < 80);
});

test("tick budget and radius follow the spec", () => {
  assert.equal(tickCount(100), 360);
  assert.equal(tickCount(200), 300);
  assert.equal(tickCount(300), 200);
  assert.equal(nodeRadius(0, true, 1), 8);
  assert.equal(nodeRadius(4, false, 1), 5 + 2 * 3.2);
});

test("over the node limit nothing is simulated", () => {
  const out = layoutForce(sample(FORCE_NODE_LIMIT + 1));
  assert.equal(out.limited, true);
  assert.equal(out.nodes.length, 0);
});

test("fixed nodes stay where they were put", () => {
  const input = sample(12);
  input.fixed = { n1: { x: 100, y: 120 } };
  const out = layoutForce(input);
  const n1 = out.nodes.find((n) => n.entityId === "n1");
  assert.deepEqual([n1.x, n1.y], [100, 120]);
});

test("the camera never magnifies past 1:1 and the transforms invert", () => {
  const layout = layoutForce(sample(10));
  const cam = fitCamera(layout, 4000, 4000);
  assert.equal(cam.k, 1);
  const [sx, sy] = worldToScreen(cam, 12, 34);
  const [wx, wy] = screenToWorld(cam, sx, sy);
  assert.ok(Math.abs(wx - 12) < 1e-9 && Math.abs(wy - 34) < 1e-9);
});

test("pickNode finds a node and misses empty space", () => {
  const layout = layoutForce(sample(10));
  const n = layout.nodes[3];
  assert.equal(pickNode(layout, n.x, n.y), n.entityId);
  assert.equal(pickNode(layout, 99999, 99999), null);
});

test("view helpers", () => {
  assert.equal(graphSummary([{ state: "down" }, { state: "up" }]), "2 devices, 1 down");
  const nodes = [{ entityId: "b", x: 1, y: 2 }, { entityId: "a", x: 5, y: 1 }];
  assert.equal(stepSelection(nodes, null, 1), "a");
  assert.equal(stepSelection(nodes, "a", 1), "b");
  assert.equal(stepSelection(nodes, "b", 1), "a");
  const cam = zoomAt({ x: 0, y: 0, k: 1 }, 100, 100, 2);
  assert.deepEqual(screenToWorld(cam, 100, 100), [100, 100]);
  const set = focusSet({ links: [{ source: "a", target: "b" }, { source: "c", target: "d" }] }, "a");
  assert.deepEqual([...set].sort(), ["a", "b"]);
  assert.equal(focusSet({ links: [] }, null), null);
});

test("colours come from tokens with a neutral fallback", () => {
  const theme = readTheme({});
  assert.equal(paletteColor(theme, 0), theme.cats[0]);
  assert.equal(paletteColor(theme, 99), theme.other);
});
