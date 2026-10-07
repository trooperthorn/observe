// Run in CI with: node --test tests/js
// The windowing and filter rules of the UniFi clients table. tests/test_unifi_pages.py runs the
// same cases through node when it is installed.
import test from "node:test";
import assert from "node:assert/strict";
import { windowFor, filterClients, attachment, clientState, markClientsStale, markCamerasStale } from "../../plugins/unifi/observe_unifi/static/vlist-core.js";

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

test("a connected client not refreshed within the window is stale, judged by the server clock", () => {
  const now = "2026-10-07T12:00:00.000Z";
  const fresh = { connected: true, last_seen: "2026-10-07T11:50:00.000Z" };
  const old = { connected: true, last_seen: "2026-10-07T11:40:00.000Z" };
  const off = { connected: false, last_seen: "2026-10-07T01:00:00.000Z" };
  const unknown = { connected: null, last_seen: "2026-10-07T01:00:00.000Z" };
  const got = markClientsStale([fresh, old, off, unknown], now, 750);
  assert.deepEqual(got.map((c) => c.stale), [false, true, false, false]);
  assert.equal(clientState(got[1]), "stale");
  assert.equal(markClientsStale([old], now, null)[0].stale, false);
  assert.equal(markClientsStale([{ connected: true, last_seen: "bad" }], now, 750)[0].stale, false);
  assert.equal(markClientsStale([fresh], Date.parse(now) / 1000 + 600, 750)[0].stale, true);  // unix seconds work too
});

test("a camera is stale when it is connected or recording and not refreshed in time", () => {
  const now = "2026-10-07T12:00:00.000Z";
  const old = "2026-10-07T11:00:00.000Z";
  const got = markCamerasStale([
    { connected: true, recording: null, last_seen: old },
    { connected: null, recording: true, last_seen: old },
    { connected: false, recording: false, last_seen: old },
    { connected: true, recording: true, last_seen: "2026-10-07T11:59:00.000Z" }], now, 300);
  assert.deepEqual(got.map((c) => c.stale), [true, true, false, false]);
});
