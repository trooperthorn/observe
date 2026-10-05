// Run in CI with: node --test tests/js
import test from "node:test";
import assert from "node:assert/strict";
import { buildGraph, defaultView, viewFromHash, linkKind } from "../../observe/static/js/graph/infra.js";

const data = {
  nodes: [
    { id: "switch:a", kind: "switch", label: "core", state: "up", anchor: true },
    { id: "switch:b", kind: "switch", label: "edge", state: "unreachable", anchor: false },
    { id: "port:a|g1", kind: "port", label: "g1", parent: "switch:a", role: "access" },
    { id: "port:b|g48", kind: "port", label: "g48", parent: "switch:b", role: "uplink" },
    { id: "port:b|g5", kind: "port", label: "g5", parent: "switch:b", role: "access" },
    { id: "endpoint:1", kind: "endpoint", label: "srv" },
  ],
  edges: [
    { a: "port:b|g48", b: "port:a|g1", source: "lldp", state: "active" },
    { a: "endpoint:1", b: "port:b|g5", source: "lldp", state: "active" },
  ],
};

test("switches become entities, endpoints become a count, anchors are kept", () => {
  const g = buildGraph(data);
  assert.equal(g.entities.length, 2);
  assert.deepEqual(g.anchors, ["switch:a"]);
  assert.equal(g.relations.length, 1);
  assert.equal(g.entities.find((e) => e.id === "switch:b").badge, "1 endpoint");
  assert.equal(g.entities.find((e) => e.id === "switch:b").state, "unreach");
});

test("the default view is tiers on phones and above 300 nodes", () => {
  assert.equal(defaultView(10, false), "graph");
  assert.equal(defaultView(10, true), "tiers");
  assert.equal(defaultView(301, false), "tiers");
  assert.equal(viewFromHash("#table"), "table");
  assert.equal(viewFromHash("#x"), null);
  assert.equal(linkKind("field_report"), "uplink");
});
