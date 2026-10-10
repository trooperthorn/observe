// Run with: node --test tests/js (the tests workflow runs this too).
import test from "node:test";
import assert from "node:assert/strict";
import { monitorCounts, summaryLabel } from "../../observe/static/js/summary-logic.js";

const WORDS = { down: "Down", unreachable: "Unreachable", warn: "Warning", pending: "Pending", up: "Up" };

test("the header counts monitors by effective state, worst first, skipping empty states", () => {
  const ms = ["up", "up", "warn", "down", "up", "unreachable"].map((s) => ({ effective_state: s }));
  assert.deepEqual(monitorCounts(ms), [["down", 1], ["unreachable", 1], ["warn", 1], ["up", 3]]);
});

test("an unknown state is counted as pending and no monitors give no chips", () => {
  assert.deepEqual(monitorCounts([{ effective_state: "odd" }, {}]), [["pending", 2]]);
  assert.deepEqual(monitorCounts([]), []);
  assert.deepEqual(monitorCounts(null), []);
});

test("the summary has a spoken label", () => {
  assert.equal(summaryLabel([["down", 2], ["warn", 1], ["up", 40]], WORDS),
    "Monitors: 2 down, 1 warning, 40 up");
  assert.equal(summaryLabel([], WORDS), "Monitors: none");
});
