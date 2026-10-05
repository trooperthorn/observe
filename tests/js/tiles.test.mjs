// Run in CI with: node --test tests/js
// Node is not needed locally; tests/test_ui_customise.py mirrors the same rules in Python.
import test from "node:test";
import assert from "node:assert/strict";
import {
  declaredTiles, effectiveHidden, effectiveOrder, layoutBody, moveTile, toggleHidden,
} from "../../observe/static/js/tiles-logic.js";

const declared = ["group:core", "group:lab", "capacity", "findings", "events"];

test("declared tiles are groups by name then the fixed cards", () => {
  assert.deepEqual(declaredTiles(["lab", "core"]), declared);
});

test("stale ids are dropped and new ids are appended", () => {
  assert.deepEqual(effectiveOrder(declared, ["events", "group:gone", "group:core", "events"]),
    ["events", "group:core", "group:lab", "capacity", "findings"]);
  assert.deepEqual(effectiveOrder(declared, null), declared);
  assert.deepEqual(effectiveOrder(declared, "nonsense"), declared);
});

test("hidden ids that no longer exist are dropped and nothing is deleted", () => {
  assert.deepEqual([...effectiveHidden(declared, ["capacity", "group:gone"])], ["capacity"]);
  assert.equal(effectiveOrder(declared, ["capacity"]).length, declared.length);
});

test("moveTile swaps neighbours, stops at the edges and does not change its input", () => {
  const o = ["a", "b", "c"];
  assert.deepEqual(moveTile(o, "b", -1), ["b", "a", "c"]);
  assert.deepEqual(moveTile(o, "b", 1), ["a", "c", "b"]);
  assert.deepEqual(moveTile(o, "a", -1), o);
  assert.deepEqual(moveTile(o, "c", 1), o);
  assert.deepEqual(moveTile(o, "x", 1), o);
  assert.deepEqual(o, ["a", "b", "c"]);
});

test("toggleHidden and layoutBody", () => {
  const h = toggleHidden(new Set(["a"]), "b", true);
  assert.deepEqual(layoutBody(["a", "b"], h), { order: ["a", "b"], hidden: ["a", "b"] });
  assert.equal(toggleHidden(h, "a", false).has("a"), false);
});
