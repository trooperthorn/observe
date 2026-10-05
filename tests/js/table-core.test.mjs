// Run in CI with: node --test tests/js
// Node is not needed locally; tests/test_ui_components.py mirrors the same rules in Python.
import test from "node:test";
import assert from "node:assert/strict";
import { sortRows, nextSort, ariaSort, pageSlice } from "../../observe/static/js/table-core.js";
import { stateInfo, STATES, ICONS } from "../../observe/static/js/chip-states.js";
import { typedMatches } from "../../observe/static/js/dialog-logic.js";

const rows = [{ n: "b", v: 2 }, { n: "a", v: null }, { n: "c", v: 1 }, { n: "d", v: 2 }];

test("sortRows is stable and sinks nulls in both directions", () => {
  assert.deepEqual(sortRows(rows, (r) => r.v, "asc").map((r) => r.n), ["c", "b", "d", "a"]);
  assert.deepEqual(sortRows(rows, (r) => r.v, "desc").map((r) => r.n), ["b", "d", "c", "a"]);
  assert.deepEqual(sortRows(rows, (r) => r.v, null).map((r) => r.n), ["b", "a", "c", "d"]);
  assert.equal(rows[0].n, "b");
});

test("sortRows orders text naturally", () => {
  const r = [{ k: "h10" }, { k: "h2" }];
  assert.deepEqual(sortRows(r, (x) => x.k, "asc").map((x) => x.k), ["h2", "h10"]);
});

test("nextSort cycles ascending, descending, none", () => {
  let s = nextSort(null, "a");
  assert.deepEqual(s, { key: "a", dir: "asc" });
  s = nextSort(s, "a");
  assert.deepEqual(s, { key: "a", dir: "desc" });
  assert.equal(nextSort(s, "a"), null);
  assert.deepEqual(nextSort(s, "b"), { key: "b", dir: "asc" });
  assert.equal(ariaSort(null, "a"), "none");
  assert.equal(ariaSort({ key: "a", dir: "desc" }, "a"), "descending");
});

test("pageSlice clamps the page and the size", () => {
  const many = Array.from({ length: 23 }, (_, i) => i);
  assert.equal(pageSlice(many, 2, 10).rows.length, 3);
  assert.equal(pageSlice(many, 9, 10).page, 2);
  assert.equal(pageSlice(many, 0, 7).size, 10);
  assert.equal(pageSlice([], 3, 10).pages, 1);
});

test("every state has a word and a known icon, and unknown states fall back", () => {
  for (const [name, info] of Object.entries(STATES)) {
    assert.ok(info.word, name);
    assert.ok(ICONS[info.icon], name);
  }
  assert.equal(stateInfo("nonsense").word, "Unknown");
  assert.equal(stateInfo("__proto__").word, "Unknown");
});

test("typedMatches needs an exact, non-empty match", () => {
  assert.equal(typedMatches("pi1", "pi1"), true);
  assert.equal(typedMatches("pi", "pi1"), false);
  assert.equal(typedMatches("", ""), false);
});
