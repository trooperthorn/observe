// Run in CI with: node --test tests/js
// Node is not needed locally; tests/test_ui_settings.py mirrors the diff rules in Python.
import test from "node:test";
import assert from "node:assert/strict";
import {
  agentText, allowlistChip, allowlistFromDraft, dataPathText, diffAllowlist, draftFromAllowlist,
  hostFromPath, settingsHref, shouldPoll, taskChip,
} from "../../observe/static/js/settings-logic.js";

const saved = () => ({
  fans: [{ header: "fan1" }, { header: "fan2", min_duty_limit: 20 }],
  services: ["smbd", "docker:scrutiny"], reboot: true, update: true,
});

test("the host name is read from the path and survives encoding", () => {
  assert.equal(hostFromPath("/hosts/nas01/settings"), "nas01");
  assert.equal(hostFromPath("/hosts/nas01/settings/"), "nas01");
  assert.equal(hostFromPath("/hosts/new"), "");
  assert.equal(hostFromPath("/hosts/a/b/settings"), "");
  assert.equal(hostFromPath("/hosts/%E0%A4%A/settings"), "");
  assert.equal(settingsHref("nas 01"), "/hosts/nas%2001/settings");
});

test("a draft round-trips to the same allowlist", () => {
  const built = allowlistFromDraft(draftFromAllowlist(saved()));
  assert.deepEqual(built.allowlist, saved());
  assert.deepEqual(diffAllowlist(saved(), built.allowlist), []);
});

test("unticked entries are removed and the diff names each change", () => {
  const draft = draftFromAllowlist(saved());
  draft.fans[0].on = false;
  draft.fans.push({ name: "fan3", on: true, limit: "30" });
  draft.services[0].on = false;
  draft.reboot = false;
  draft.update = false;
  const built = allowlistFromDraft(draft);
  assert.deepEqual(diffAllowlist(saved(), built.allowlist), [
    "Add fan header fan3 (lowest duty 30%)", "Remove fan header fan1", "Remove service smbd",
    "Do not allow reboot", "Do not allow agent updates from Observe",
  ]);
  // A host saved before agent updates existed reads as off, so ticking it is a change.
  const old = draftFromAllowlist({ fans: [], services: [], reboot: false });
  assert.equal(old.update, false);
  old.update = true;
  assert.deepEqual(diffAllowlist({ fans: [], services: [], reboot: false }, allowlistFromDraft(old).allowlist),
    ["Allow agent updates from Observe"]);
});

test("a changed limit is a change, and bad entries are refused", () => {
  const draft = draftFromAllowlist(saved());
  draft.fans[1].limit = "35";
  assert.deepEqual(diffAllowlist(saved(), allowlistFromDraft(draft).allowlist), [
    "Change fan header fan2: lowest duty 20% to lowest duty 35%",
  ]);
  draft.fans[1].limit = "101";
  assert.ok(allowlistFromDraft(draft).error);
  draft.fans[1].limit = "";
  draft.services.push({ name: "a;b", on: true });
  assert.ok(allowlistFromDraft(draft).error);
});

test("status words never rely on colour and unknown states are labelled", () => {
  assert.deepEqual(allowlistChip("applied"), ["up", "Applied"]);
  assert.deepEqual(allowlistChip("pending"), ["pending", "Pending"]);
  assert.deepEqual(allowlistChip("nope"), ["pending", "Unknown"]);
  assert.deepEqual(taskChip("failed"), ["down", "Failed"]);
});

test("polling goes on only while something can still change", () => {
  assert.ok(shouldPoll({ task: { state: "waiting" } }, false));
  assert.ok(shouldPoll({ task: { state: "fetched" } }, false));
  assert.ok(!shouldPoll({ task: { state: "done" } }, false));
  assert.ok(!shouldPoll({ task: null }, false));
  assert.ok(shouldPoll({ task: null }, true));
  assert.ok(shouldPoll({ task: null, enrolment: { token_state: "valid" } }, false));
  assert.ok(!shouldPoll({ task: null, enrolment: { token_state: "used" } }, false));
});

test("a host Observe polls says so instead of a fake agent version and a bare no keys", () => {
  const ha = { enrolled: false, agent_version: "observe-ha-host",
    agent_label: "polled by Observe (Home Assistant monitor)", active_keys: 0,
    polled_by: [{ monitor: "HA host", kind: "Home Assistant", credential: "ha_reader" }] };
  assert.equal(agentText(ha), "polled by Observe (Home Assistant monitor)");
  assert.match(dataPathText(ha), /^Polled by Observe: Home Assistant monitor HA host \(credential ha_reader\)\./);
  assert.match(dataPathText(ha), /no ingest key is needed/);
  // A pushed agent keeps its version, and has no polling line.
  const nas = { enrolled: true, agent: true, agent_version: "0.9.0", agent_label: "", polled_by: [] };
  assert.equal(agentText(nas), "hostwatch 0.9.0");
  assert.equal(dataPathText(nas), "");
  assert.equal(agentText({ enrolled: true, agent: true }), "hostwatch, not reporting yet");
  assert.equal(agentText({ enrolled: true, agent: false }), "not chosen");
  assert.equal(agentText({ enrolled: false }), "unknown");
});
