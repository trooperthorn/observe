// Run in CI with: node --test tests/js
// The windowing and filter rules of the UniFi clients table. tests/test_unifi_pages.py runs the
// same cases through node when it is installed.
import test from "node:test";
import assert from "node:assert/strict";
import { windowFor, filterClients, attachment, clientState, markClientsStale, markCamerasStale, vlanOptions, ssidOptions, uptimeSeconds, defaultSsid, permittedApsText, carryingText, bandText, lastSeenCell, siteText, internetTile, ssidBarRows } from "../../plugins/unifi/observe_unifi/static/vlist-core.js";

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

test("the VLAN and SSID filters match exactly and the option lists are sorted", () => {
  const rows = [
    { name: "a", vlan: 30, ssid: "IoT", kind: "wireless", connected: true },
    { name: "b", vlan: 1, ssid: "Home", kind: "wireless", connected: true },
    { name: "c", vlan: null, ssid: "", kind: "wired", connected: true, network: "LAN" },
    { name: "d", vlan: 300, ssid: "IoT", kind: "wireless", connected: false }];
  assert.deepEqual(vlanOptions(rows), ["1", "30", "300"]);
  assert.deepEqual(ssidOptions(rows), ["Home", "IoT"]);
  assert.deepEqual(filterClients(rows, { vlan: "30" }).map((c) => c.name), ["a"]);  // not 300
  assert.deepEqual(filterClients(rows, { ssid: "IoT" }).map((c) => c.name), ["a", "d"]);
  assert.deepEqual(filterClients(rows, { ssid: "IoT", state: "connected" }).map((c) => c.name), ["a"]);
  assert.deepEqual(filterClients(rows, { q: "lan" }).map((c) => c.name), ["c"]);  // the network name
  assert.equal(filterClients(rows, { vlan: "" }).length, 4);
});

test("uptime is the classic value, else the time since connected_at, else unknown", () => {
  const now = "2026-10-07T12:00:00.000Z";
  assert.equal(uptimeSeconds({ connected: true, uptime_s: 90, connected_at: "2026-10-07T11:00:00.000Z" }, now), 90);
  assert.equal(uptimeSeconds({ connected: true, uptime_s: null, connected_at: "2026-10-07T11:00:00.000Z" }, now), 3600);
  assert.equal(uptimeSeconds({ connected: true, uptime_s: null, connected_at: null }, now), null);
  assert.equal(uptimeSeconds({ connected: true, connected_at: "2026-10-07T13:00:00.000Z" }, now), null);  // the future
  assert.equal(uptimeSeconds({ connected: false, uptime_s: 90 }, now), null);
  assert.equal(uptimeSeconds({ connected: null, connected_at: "2026-10-07T11:00:00.000Z" }, now), null);
});

test("the Wi-Fi words: default SSID, permitted and carrying access points, bands", () => {
  assert.equal(defaultSsid([{ name: "Home" }, { name: "WiFIoT" }]), "WiFIoT");
  assert.equal(defaultSsid([{ name: "Home" }]), "");
  assert.equal(permittedApsText({ ap_group_mode: "all" }), "Every access point");
  assert.equal(permittedApsText({ ap_group_mode: "" }), "Every access point");
  assert.equal(permittedApsText({ ap_group_mode: "specific", ap_names: ["Attic", "Shed"] }), "Attic, Shed");
  assert.match(permittedApsText({ ap_group_mode: "specific", ap_names: [] }), /cannot be named/);
  assert.equal(carryingText({ carrying_aps: [], client_count: 0 }), "none");
  assert.equal(carryingText({ carrying_aps: ["Attic"], client_count: 1 }), "Attic (1 client)");
  assert.equal(carryingText({ carrying_aps: [], client_count: 2 }), "an access point the poll did not name (2 clients)");
  assert.equal(bandText("both"), "2.4 GHz, 5 GHz");
  assert.equal(bandText("2g"), "2.4 GHz");
  assert.equal(bandText("5g"), "5 GHz");
  assert.equal(bandText(""), "Not reported");
});

test("a connected client is seen now; its connect time is never shown as last seen", () => {
  // The reported row: connected, connected_at = now minus uptime, shown under "Last seen".
  assert.deepEqual(lastSeenCell({ connected: true, connected_at: 900, last_seen: null }), { now: true });
  assert.deepEqual(lastSeenCell({ connected: true, stale: true, connected_at: 900, last_seen: 950 }), { at: 950 });
  assert.deepEqual(lastSeenCell({ connected: false, last_seen: 800 }), { at: 800 });
  assert.deepEqual(lastSeenCell({ connected: null }), { at: null });
});

test("the overview names the site, hides an unknown Internet tile and unnamed SSID bars", () => {
  assert.equal(siteText({ site_id: "88f7af54-98f8-306a-a1c7-c9349722b1f6", site_name: "Default" }), "site Default");
  assert.equal(siteText({ site_id: "abc", site_name: "" }), "site abc");
  assert.equal(siteText({ site_id: null }), "no site polled yet");
  assert.equal(internetTile({ internet_up: null }), null);
  assert.deepEqual(internetTile({ internet_up: true, internet_source: "wan_traffic" }), { up: true, inferred: true });
  assert.deepEqual(internetTile({ internet_up: false, internet_source: "console" }), { up: false, inferred: false });
  assert.deepEqual(ssidBarRows({ clients_per_ssid: [] }), []);
  assert.deepEqual(ssidBarRows({ clients_per_ssid: [{ ssid: "", count: 3 }, { ssid: "IoT", count: 2 }] }),
    [{ ssid: "IoT", count: 2 }]);
  assert.deepEqual(ssidBarRows({}), []);
});
