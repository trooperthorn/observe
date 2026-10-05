// Run in CI with: node --test tests/js
// Node is not needed locally; tests/test_ui_wizard.py checks the same rules against the server.
import test from "node:test";
import assert from "node:assert/strict";
import {
  buildBody, controlBlock, defaultFans, parseLimit, progressChip, stepFromHash, validHeader,
  validName, validPool, validService, hostHref, reportChip,
} from "../../observe/static/js/wizard-logic.js";

const base = () => ({
  name: "nas01", platform: "linux", pool: "", control: false, reboot: false,
  fans: defaultFans("linux"), services: [{ name: "smbd", on: true }, { name: "nfs-server", on: false }],
});

test("host names follow the server rule", () => {
  for (const ok of ["a", "nas01", "nas-01", "0a"]) assert.ok(validName(ok), ok);
  for (const bad of ["", "NAS01", "-a", "a-", "a b", "a_b", '<img src=x onerror="alert(1)">', "a".repeat(64)]) {
    assert.ok(!validName(bad), bad);
  }
});

test("header, service and pool names", () => {
  assert.ok(validHeader("pwm-fan") && !validHeader("a b") && !validHeader("x".repeat(33)));
  assert.ok(validService("smbd") && validService("docker:scrutiny"));
  assert.ok(!validService("a..b") && !validService("-x") && !validService("a@b") && !validService("a;b"));
  assert.ok(validPool("") && validPool("Apps") && !validPool("a/b") && !validPool("a..b"));
});

test("limits are blank or a whole number from 0 to 100", () => {
  assert.equal(parseLimit(""), null);
  assert.equal(parseLimit(" 20 "), 20);
  assert.equal(parseLimit("100"), 100);
  for (const bad of ["101", "-1", "2.5", "x", "1e2"]) assert.equal(parseLimit(bad), undefined, bad);
});

test("control is blocked with a written reason on Windows and TrueNAS only", () => {
  assert.match(controlBlock("windows"), /Windows/);
  assert.match(controlBlock("truenas"), /TrueNAS/);
  assert.equal(controlBlock("linux"), "");
  assert.equal(controlBlock("raspberry-pi"), "");
});

test("Raspberry Pi pre-selects pwm-fan", () => {
  assert.deepEqual(defaultFans("raspberry-pi"), [{ name: "pwm-fan", on: true, limit: "" }]);
});

test("buildBody leaves the allowlist out when control is off", () => {
  const { body } = buildBody(base());
  assert.deepEqual(body, { name: "nas01", platform: "linux", agent: true, control: false });
});

test("buildBody sends the ticked entries and per-header limits", () => {
  const s = { ...base(), control: true, reboot: true };
  s.fans[1].limit = "20";
  const { body } = buildBody(s);
  assert.deepEqual(body.allowlist, {
    fans: ["fan1", { header: "fan2", min_duty_limit: 20 }], services: ["smbd"], reboot: true,
  });
});

test("buildBody refuses control on a platform without it, and bad input", () => {
  assert.equal(buildBody({ ...base(), platform: "windows", control: true }).body.control, false);
  assert.ok(buildBody({ ...base(), name: "BAD" }).error);
  const s = { ...base(), control: true };
  s.fans[0].limit = "500";
  assert.ok(buildBody(s).error);
  assert.ok(buildBody({ ...base(), platform: "truenas", pool: "a/b" }).error);
  assert.equal(buildBody({ ...base(), platform: "truenas", pool: "Tank" }).body.pool, "Tank");
});

test("the step comes from the hash and needs a created host for the last two", () => {
  assert.equal(stepFromHash("#agent", null), "agent");
  assert.equal(stepFromHash("#install", null), "host");
  assert.equal(stepFromHash("#live", { host: "a" }), "live");
  assert.equal(stepFromHash("#nonsense", { host: "a" }), "host");
  assert.equal(stepFromHash("", null), "host");
});

test("every progress and report status has an icon word, never colour alone", () => {
  for (const s of ["done", "waiting", "skipped", "expired"]) assert.ok(progressChip(s)[1].length > 0);
  assert.deepEqual(progressChip("wat"), ["pending", "Unknown"]);
  assert.deepEqual(reportChip("refused"), ["down", "Refused"]);
});

test("the host link encodes the name", () => {
  assert.equal(hostHref("nas01"), "/host?name=nas01");
  assert.equal(hostHref("a&b"), "/host?name=a%26b");
});
