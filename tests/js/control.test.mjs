// Run in CI with: node --test tests/js
// The Control card's choices (observe/static/js/control-logic.js) from a capabilities answer.
import test from "node:test";
import assert from "node:assert/strict";
import {
  clampDuty, componentChoices, controlShown, controllerChoices, defaultDuty, floorOf, floorText,
  formActions, headerChoices, serviceChoices,
} from "../../observe/static/js/control-logic.js";

// mediain-svr: enrolled with control, fans fan1 and fan2 at a floor of 30, two services.
const fan = (header, controller_id) => ({ header, controller_id, min_duty_limit: 30, floor: 30 });
const mediain = {
  host: "mediain-svr", known: true, available: true, reason: "",
  actions: ["fan.set_floor", "service.restart", "host.reboot", "agent.update"],
  controller: "thermalctl", controllers: ["thermalctl"], modes: ["dry_run", "active"],
  components: ["agent"],
  capabilities: { thermalctl: true, headers: ["pwm1", "pwm2", "pwm3", "pwm4"] },
  allowlist: { fans: [fan("fan1", "pwm1"), fan("fan2", "pwm2")], services: ["smbd", "nfs-server"],
    reboot: true, update: true, min_duty_floor: 30 },
  fan_headers: [fan("fan1", "pwm1"), fan("fan2", "pwm2")],
};
// homeassistant: no control daemon.
const none = { host: "homeassistant", known: true, available: false,
  reason: "this host has no control daemon", actions: [], controller: "thermalctl",
  controllers: ["thermalctl"], components: ["agent", "control", "all"], allowlist: null,
  fan_headers: null };

test("the card is drawn only for a host with a control daemon", () => {
  assert.equal(controlShown(mediain), true);
  assert.equal(controlShown(none), false);
  assert.equal(controlShown({ ...mediain, known: false }), false);
  assert.equal(controlShown({ ...mediain, actions: [] }), false);
  assert.equal(controlShown(null), false);
});

test("the action list and the controller come from the answer", () => {
  assert.deepEqual(formActions(mediain), ["fan.set_floor", "service.restart", "agent.update"]);
  assert.deepEqual(controllerChoices(mediain), ["thermalctl"]);
  assert.deepEqual(controllerChoices({ controllers: ["a", "b"] }), ["a", "b"]);
  assert.deepEqual(componentChoices(mediain), ["agent"]);
});

test("headers are the allowlisted ones, sent by controller id, labelled with both names", () => {
  const headers = headerChoices(mediain);
  assert.deepEqual(headers, [
    { value: "pwm1", label: "fan1 (pwm1)", floor: 30 },
    { value: "pwm2", label: "fan2 (pwm2)", floor: 30 },
  ]);
  assert.equal(headerChoices(none), null);
  assert.deepEqual(headerChoices({ fan_headers: [{ header: "pwm-fan", controller_id: "pwm-fan",
    floor: 0 }] }), [{ value: "pwm-fan", label: "pwm-fan", floor: 0 }]);
});

test("services are the allowlisted ones, or free text when the allowlist is not held", () => {
  assert.deepEqual(serviceChoices(mediain), ["smbd", "nfs-server"]);
  assert.equal(serviceChoices(none), null);
});

test("minimum duty starts at and is clamped to the header's floor", () => {
  const headers = headerChoices(mediain);
  const floor = floorOf(headers, "pwm1");
  assert.equal(floor, 30);
  assert.equal(floorOf(headers, "pwm9"), 0);
  assert.equal(defaultDuty(floor), 30);
  assert.equal(defaultDuty(0), 20);
  for (const [text, want] of [["20", 30], ["0", 30], ["-5", 30], ["", 30], ["abc", 30],
    ["45", 45], ["44.6", 45], ["150", 100], ["30", 30]]) {
    assert.equal(clampDuty(text, floor), want, text);
  }
  assert.equal(clampDuty("0", 0), 0);
  assert.equal(floorText(30), "This host refuses a floor under 30%.");
  assert.equal(floorText(0), "");
});
