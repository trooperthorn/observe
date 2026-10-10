// Run in CI with: node --test tests/js
// tests/test_ui_hosts.py runs this file too when Node is on the path.
import test from "node:test";
import assert from "node:assert/strict";
import {
  STATUS_STATE, ageText, filterRows, hostRow, hostRows, summaryText, waitingRow,
} from "../../observe/static/js/hosts-logic.js";

const nas = { host: "nas01", platform: "linux", agent_version: "0.9.0", heard: true, age_seconds: 12,
  stale: false, monitored: true, monitor: { name: "nas", effective_state: "up" }, status: "good",
  status_reason: "" };
const silent = { host: "rack/sw 01", platform: "", agent_version: "", heard: false, age_seconds: null,
  stale: false, monitored: true, monitor: { name: "rack/sw 01" }, status: "critical",
  status_reason: "no batch received yet" };
const stray = { host: "pi", platform: "raspberry-pi", agent_version: "0.8.0", heard: true,
  age_seconds: 7200, stale: true, monitored: false, monitor: null, status: "warning",
  status_reason: "stale" };
const waiting = { host: "newbox", platform: "windows", control: false, created: 1, state: "waiting",
  note: "command not run yet", enrolment_url: "/hosts/newbox/settings" };

test("every status maps to a chip state", () => {
  assert.deepEqual(STATUS_STATE, { good: "up", warning: "warn", critical: "down", waiting: "pending" });
});

test("ageText says never for a host that has not reported", () => {
  assert.equal(ageText(null), "never");
  assert.equal(ageText(12), "12s ago");
  assert.equal(ageText(600), "10m ago");
  assert.equal(ageText(7200), "2h ago");
  assert.equal(ageText(200000), "2d ago");
});

test("a host row carries the page link, the settings link and the monitor listing", () => {
  const r = hostRow(nas);
  assert.equal(r.href, "/host?name=nas01");
  assert.equal(r.settings, "/hosts/nas01/settings");
  assert.equal(r.monitored, true);
  assert.equal(r.monitor, "nas");
  assert.equal(r.age, 12);
  assert.equal(r.waiting, false);
  const s = hostRow(silent);
  assert.equal(s.href, "/host?name=rack%2Fsw%2001");
  assert.equal(s.age, null);  // never heard, so the age column sinks in both sort directions
  assert.equal(s.detail, "no batch received yet");
});

test("a waiting row links only to the enrolment page", () => {
  const w = waitingRow(waiting);
  assert.equal(w.href, null);
  assert.equal(w.settings, "/hosts/newbox/settings");
  assert.equal(w.status, "waiting");
  assert.equal(w.detail, "command not run yet");
  assert.equal(w.waiting, true);
});

test("hostRows appends waiting hosts the list does not already name", () => {
  const rows = hostRows([nas, stray], [waiting, { ...waiting, host: "nas01" }]);
  assert.deepEqual(rows.map((r) => r.name), ["nas01", "pi", "newbox"]);
  assert.deepEqual(rows.map((r) => r.statusRank), [2, 1, 3]);
  assert.deepEqual(hostRows(null, null), []);
});

test("filterRows matches the name or the platform without case", () => {
  const rows = hostRows([nas, stray], [waiting]);
  assert.deepEqual(filterRows(rows, "  NAS ").map((r) => r.name), ["nas01"]);
  assert.deepEqual(filterRows(rows, "win").map((r) => r.name), ["newbox"]);
  assert.equal(filterRows(rows, "").length, 3);
});

test("summaryText counts hosts, attention and waiting", () => {
  assert.equal(summaryText([]), "0 hosts");
  assert.equal(summaryText(hostRows([nas], [])), "1 host");
  assert.equal(summaryText(hostRows([nas, stray, silent], [waiting])),
    "4 hosts, 2 need attention, 1 waiting for first data");
});
