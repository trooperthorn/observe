// Run in CI with: node --test tests/js
// The admin settings pages as pure functions (observe/static/js/admin-settings-logic.js): the
// body each form sends, the rows a document becomes, and the words for a rule and the storage.
import test from "node:test";
import assert from "node:assert/strict";
import {
  backendText, levelName, levelOf, numberOrNull, recheckBody, retentionBody, rollupText, ruleFromFields,
  ruleSummary, tierHostRows, tiersBody,
} from "../../observe/static/js/admin-settings-logic.js";

test("numberOrNull reads a number, an empty cell as nothing, and refuses text", () => {
  assert.equal(numberOrNull(""), null);
  assert.equal(numberOrNull("  "), null);
  assert.equal(numberOrNull(undefined), null);
  assert.equal(numberOrNull("30"), 30);
  assert.equal(numberOrNull(" 2.5 "), 2.5);
  assert.throws(() => numberOrNull("abc"), /not a number/);
  assert.throws(() => numberOrNull("Infinity"), /not a number/);
});

test("tiersBody resets an empty global cell, drops empty host cells and hosts with none", () => {
  const body = tiersBody(
    { availability: "20", smart: "" },
    [{ host: "nas01", values: { availability: "10", smart: "" } }, { host: "nas02", values: { smart: "" } }, { host: " ", values: { smart: "5" } }]);
  assert.deepEqual(body, { global: { availability: 20, smart: null }, hosts: { nas01: { availability: 10 } } });
});

test("tierHostRows lists overridden hosts first, then the other known hosts once", () => {
  const rows = tierHostRows({ hosts: { b: { smart: 600 } }, known_hosts: ["a", "b", "c"] });
  assert.deepEqual(rows, [{ host: "b", values: { smart: 600 } }, { host: "a", values: {} }, { host: "c", values: {} }]);
  assert.deepEqual(tierHostRows({}), []);
});

test("recheckBody keeps a null for a global reset and only the cells that are set", () => {
  const body = recheckBody({ window: "", interval: "15", good: "3" }, [
    { slug: "p", values: { window: "0", good: "" } }, { slug: "q", values: { window: "", good: "" } }]);
  assert.deepEqual(body, { window: null, interval: 15, good: 3, overrides: { p: { window: 0 } } });
});

test("retentionBody needs every global number and ignores an override row with no metric", () => {
  const body = retentionBody({ raw_days: "30", hourly_days: "90" }, [
    { metric: " temp ", values: { raw_days: "2", hourly_days: "" } }, { metric: "", values: { raw_days: "9" } }]);
  assert.deepEqual(body, { raw_days: 30, hourly_days: 90, overrides: { temp: { raw_days: 2 } } });
  assert.throws(() => retentionBody({ raw_days: "" }, []), /raw_days needs a number of days/);
  assert.deepEqual(retentionBody({ raw_days: "30" }, []).overrides, {});
});

test("levelOf reads one number, or a pair for an outside rule", () => {
  assert.equal(levelOf("80", "above"), 80);
  assert.equal(levelOf("", "above"), null);
  assert.deepEqual(levelOf("10, 90", "outside"), [10, 90]);
  assert.throws(() => levelOf("10", "outside"), /pair/);
  assert.throws(() => levelOf("1, 2, 3", "outside"), /pair/);
});

test("ruleFromFields sends only the fields the kind takes", () => {
  const base = { id: " r1 ", metric: "cpu.temp", host: "", warn: "70", crit: "85", condition: "above", clear: "" };
  assert.deepEqual(ruleFromFields({ ...base, kind: "consecutive", x: "3" }), {
    id: "r1", kind: "consecutive", metric: "cpu.temp", host: "", enabled: true, condition: "above",
    warn: 70, crit: 85, missing: "unknown", x: 3 });
  assert.deepEqual(ruleFromFields({ ...base, kind: "ratio", x: "2", y: "5", clear: "4", missing: "breaching" }), {
    id: "r1", kind: "ratio", metric: "cpu.temp", host: "", enabled: true, condition: "above",
    warn: 70, crit: 85, missing: "breaching", x: 2, y: 5, clear: 4 });
  const w = ruleFromFields({ ...base, kind: "window", window: "300", agg: "max", x: "9" });
  assert.equal(w.window, 300);
  assert.equal(w.agg, "max");
  assert.ok(!("x" in w) && !("y" in w));
  const out = ruleFromFields({ ...base, kind: "window", condition: "outside", warn: "10,90", crit: "5,95", window: "60" });
  assert.deepEqual([out.warn, out.crit], [[10, 90], [5, 95]]);
});

test("a missing-data rule takes a gap or x and y, and never a condition or levels", () => {
  const gap = ruleFromFields({ id: "m", kind: "missing", metric: "m", gap: "600", severity: "critical", warn: "5", condition: "above" });
  assert.deepEqual(gap, { id: "m", kind: "missing", metric: "m", host: "", enabled: true, severity: "critical", gap: 600 });
  const ratio = ruleFromFields({ id: "m", kind: "missing", metric: "m", gap: "", x: "2", y: "4" });
  assert.deepEqual(ratio, { id: "m", kind: "missing", metric: "m", host: "", enabled: true, severity: "warning", x: 2, y: 4 });
});

test("ruleSummary says what each kind of saved rule does", () => {
  assert.equal(ruleSummary({ id: "a", kind: "consecutive", metric: "cpu", host: "", condition: "above", warn: 70, crit: 85, x: 3 }),
    "cpu on every host: above, warn 70, crit 85, 3 samples in a row");
  assert.equal(ruleSummary({ id: "b", kind: "ratio", metric: "cpu", host: "nas", condition: "below", warn: null, crit: 5, x: 2, y: 5 }),
    "cpu on nas: below, warn none, crit 5, 2 of the last 5 samples");
  assert.equal(ruleSummary({ id: "c", kind: "window", metric: "cpu", host: "", condition: "outside", warn: [10, 90], crit: null, window: 60, agg: "avg" }),
    "cpu on every host: outside, warn 10 to 90, crit none, avg over 60 seconds");
  assert.equal(ruleSummary({ id: "d", kind: "missing", metric: "cpu", host: "", severity: "warning", gap: 300 }),
    "cpu on every host: no sample for 300 seconds, warning");
  assert.equal(ruleSummary({ id: "e", kind: "missing", metric: "cpu", host: "", severity: "critical", x: 2, y: 4 }),
    "cpu on every host: 2 of the last 4 polls empty, critical");
});

test("the storage words name the backend and who runs the rollups", () => {
  assert.equal(backendText({ backend: "sqlite" }), "SQLite");
  assert.equal(backendText({ backend: "postgres", timescaledb: false }), "PostgreSQL");
  assert.equal(backendText({ backend: "postgres", timescaledb: true }), "PostgreSQL with TimescaleDB");
  assert.match(rollupText({ backend: "sqlite" }), /application keeps/);
  assert.match(rollupText({ backend: "postgres", timescaledb: true }), /TimescaleDB runs/);
  assert.match(rollupText({ backend: "postgres", timescaledb: false, incremental_rollups: false }), /on a schedule/);
  assert.equal(levelName("1h"), "Trim hourly summaries");
  assert.equal(levelName("odd"), "odd");
});
