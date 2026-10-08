// Run with: node --test tests/js (the tests workflow runs this too).
import test from "node:test";
import assert from "node:assert/strict";
import { formatValue, formatReading } from "../../observe/static/js/format.js";

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
  assert.equal(formatValue(41.234, "Cel"), "41.23 Cel");
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
