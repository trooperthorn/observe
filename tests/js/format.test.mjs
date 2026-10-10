// Run with: node --test tests/js (the tests workflow runs this too).
import test from "node:test";
import assert from "node:assert/strict";
import { eventText, formatValue, formatReading, monitorReading, plural } from "../../observe/static/js/format.js";

test("a ratio of unit 1 is shown as a percentage", () => {
  assert.equal(formatValue(0.35, "1"), "35 %");
  assert.equal(formatValue(0.3567, "1"), "35.7 %");
  assert.equal(formatValue(1.5, "1"), "150 %");
});

test("bytes use binary prefixes", () => {
  assert.equal(formatValue(8e9, "By"), "7.45 GiB");
  assert.equal(formatValue(512, "By"), "512 B");
  assert.equal(formatValue(1536, "By"), "1.50 KiB");
  assert.equal(formatValue(3 * 1024 ** 4, "By"), "3.00 TiB");
});

test("hertz, bit/s and By/s use SI prefixes", () => {
  assert.equal(formatValue(2.4e9, "Hz"), "2.40 GHz");
  assert.equal(formatValue(800e6, "Hz"), "800 MHz");
  assert.equal(formatValue(1e9, "bit/s"), "1.00 Gbit/s");
  assert.equal(formatValue(12.5e6, "By/s"), "12.5 MBy/s");
});

test("seconds become durations", () => {
  assert.equal(formatValue(0.25, "s"), "250 ms");
  assert.equal(formatValue(42, "s"), "42 s");
  assert.equal(formatValue(3725, "s"), "1h 2m 5s");
  assert.equal(formatValue(90061, "s"), "1d 1h 1m");
});

test("large readings never use exponent notation", () => {
  for (const u of ["", "W", "By", "Hz", "1", "s", "bit/s"]) {
    assert.doesNotMatch(formatValue(3.2e12, u), /e[+-]?\d/);
  }
  assert.equal(formatValue(5e6, "W"), "5000000 W");
});

test("unknown units keep the number and unit, missing values are empty", () => {
  assert.equal(formatValue(41.234, "furlong"), "41.23 furlong");
  assert.equal(formatValue(41.234, "Cel"), "41.23°C");  // bug plan WP8: no raw "Cel"
  assert.equal(formatValue(null, "By"), "");
});

test("values just under a step roll over to the next prefix", () => {
  assert.equal(formatValue(1023.9, "By"), "1.00 KiB");
  assert.equal(formatValue(1023.99 * 1024, "By"), "1.00 MiB");
  assert.equal(formatValue(999999, "Hz"), "1.00 MHz");
  assert.equal(formatValue(59.999, "s"), "1m");
  assert.equal(formatValue(0.9996, "s"), "1 s");
});

test("huge plain values never use exponent notation", () => {
  assert.doesNotMatch(formatValue(1e21, ""), /e/);
  assert.doesNotMatch(formatValue(1e25, "W"), /e/);
});

test("values that are not finite numbers are shown as received", () => {
  assert.equal(formatValue(NaN, "W"), "NaN W");
  assert.equal(formatValue(Infinity, ""), "Infinity");
  assert.equal(formatValue("12", "W"), "12 W");
});

test("monitor units with a leading space keep the one-decimal display", () => {
  assert.equal(formatReading(12.345, " ms"), "12.3 ms");
  assert.equal(formatReading(1.234, " days"), "1.2 days");
  assert.equal(formatReading(250.4, " ms"), "250 ms");
  assert.equal(formatReading(41.27, "%"), "41.3%");
  assert.equal(formatReading(7, ""), "7");
  assert.equal(formatReading(0.35, "1"), "35 %");
  assert.equal(formatReading(8e9, "By"), "7.45 GiB");
});

test("count units in curly braces are shown as words", () => {
  assert.equal(formatReading(3682, "{entity}"), "3682 entities");
  assert.equal(formatReading(586, "{entity unavailable}"), "586 entities unavailable");
  assert.equal(formatReading(1, "{update}"), "1 update");
  assert.equal(formatReading(0, "{update}"), "0 updates");
  assert.equal(formatReading(2, "{device offline}"), "2 devices offline");
  assert.equal(formatReading(1, "{camera disconnected}"), "1 camera disconnected");
  assert.equal(formatValue(4, "{}"), "4");
  assert.deepEqual(["entity", "day", "box", "switch", "alert"].map(plural),
    ["entities", "days", "boxes", "switches", "alerts"]);
});

test("a monitor card value always says what it is", () => {
  assert.equal(monitorReading({ type: "pushed_host", value: 67, unit: "s", result: "ok" }),
    "data 1m 7s old");
  assert.equal(monitorReading({ type: "home_assistant", value: 586, unit: "{entity unavailable}" }),
    "586 entities unavailable");
  assert.equal(monitorReading({ type: "technitium", value: 2.1, unit: "%" }), "2.1%");
  assert.equal(monitorReading({ type: "http", value: null, latency_ms: 41.6 }), "42 ms");
  assert.equal(monitorReading({ type: "http", value: null, latency_ms: 41.6, result: "fail" }), "");
  assert.equal(monitorReading({ type: "http", value: null, latency_ms: null }), "");
});

test("an event row names the monitor by its display name", () => {
  const e = {
    event_name: "observe.monitor.transition", body: "2.1% SERVFAIL",
    resource: { name: "dns1-servfail-rate" },
    attributes: { "observe.monitor.state.previous": "up", "observe.monitor.state": "warn" },
  };
  const names = new Map([["dns1-servfail-rate", "DNS1 SERVFAIL rate"]]);
  assert.equal(eventText(e, names), "DNS1 SERVFAIL rate: up → warn (2.1% SERVFAIL)");
  assert.equal(eventText(e, new Map()), "dns1-servfail-rate: up → warn (2.1% SERVFAIL)");
  assert.equal(eventText({ event_name: "x.y", body: "b", resource: { name: "h" } }, null), "h: x.y (b)");
});

test("one date format everywhere, with the year, from seconds, strings or dates", async () => {
  const { formatWhen, refreshedText } = await import("../../observe/static/js/format.js");
  const s = 1_791_642_785;  // 2026-10-10
  const iso = new Date(s * 1000).toISOString();
  assert.equal(formatWhen(s, "en-US"), formatWhen(iso, "en-US"));
  assert.equal(formatWhen(new Date(s * 1000), "en-US"), formatWhen(s, "en-US"));
  assert.match(formatWhen(s, "en-US"), /2026/);
  assert.equal(formatWhen(null), "never");
  assert.equal(formatWhen("junk"), "never");
  assert.match(refreshedText(new Date(s * 1000)), /^refreshed .*2026/);
});

test("agent unit codes read as words, and state gauges as their state", async () => {
  const { formatValue, readingText } = await import("../../observe/static/js/format.js");
  assert.equal(formatValue(41, "Cel"), "41°C");
  assert.equal(formatValue(1200, "{rpm}"), "1200 RPM");
  assert.equal(formatValue(0.5, "{thread}"), "0.5");
  assert.equal(formatValue(3, "{count}"), "3");
  assert.equal(readingText({ metric: "hw.status", value: 1, unit: "1", labels: { "hw.state": "clean" } }), "clean");
  assert.equal(readingText({ metric: "hw.status", value: 0, unit: "1", labels: { "hw.state": "degraded" } }), "not degraded");
  assert.equal(readingText({ metric: "observe.thermal.mode", value: 1, unit: "1",
    labels: { "observe.thermal.mode": "auto" } }), "auto");
  assert.equal(readingText({ metric: "system.cpu.utilization", value: 0.12, unit: "1", labels: {} }), "12 %");
  assert.equal(readingText({ metric: "hw.temperature", value: null }), "no value");
});
