// Run in CI with: node --test tests/js
// The windowing and filter rules of the UniFi clients table. tests/test_unifi_pages.py runs the
// same cases through node when it is installed.
import test from "node:test";
import assert from "node:assert/strict";
import { windowFor, filterClients, attachment, clientState } from "../../plugins/unifi/observe_unifi/static/vlist-core.js";

const rows = Array.from({ length: 500 }, (_, i) => ({
  name: `c${i}`, mac: `m${i}`, ip: "", ssid: "", uplink_name: i % 2 ? "AP" : "SW",
  kind: i % 2 ? "wireless" : "wired", connected: i % 5 !== 0, sw_port: i % 2 ? null : 3 }));

test("windowFor draws a slice and pads the rest so the scroll bar spans the list", () => {
  assert.deepEqual(windowFor(4000, 480, 40, 500), { start: 94, end: 118, top: 3760, bottom: 15280 });
  assert.equal(windowFor(0, 480, 40, 500).start, 0);
  const end = windowFor(99999, 480, 40, 500);
  assert.equal(end.end, 500);
  assert.equal(end.bottom, 0);
  assert.deepEqual(windowFor(0, 480, 40, 0), { start: 0, end: 0, top: 0, bottom: 0 });
});

test("filters combine and match without regard to case", () => {
  assert.equal(filterClients(rows, { q: "C42" }).length, 11);
  assert.equal(filterClients(rows, { kind: "wired" }).length, 250);
  assert.equal(filterClients(rows, { state: "offline" }).length, 100);
  assert.equal(filterClients(rows, { kind: "wireless", state: "connected", q: "ap" }).length, 200);
});

test("a client with no state is unknown and attachment names the port", () => {
  assert.equal(clientState({ connected: null }), "unknown");
  assert.equal(attachment(rows[0]), "SW port 3");
  assert.equal(attachment(rows[1]), "AP");
});
