// Run in CI with: node --test tests/js
// The Updates page's rules and the agent.update result reader (observe/static/js/updates-logic.js).
import test from "node:test";
import assert from "node:assert/strict";
import {
  PHASES, agentRowState, overallChip, phaseChips, polling, pullAgeText, updateAllSummary,
  updateResultText, upstreamText, versionChanged,
} from "../../observe/static/js/updates-logic.js";

test("phase chips mark done, running and not yet reached, and never colour alone", () => {
  const chips = phaseChips({ phase: "build" });
  assert.deepEqual(chips.map((c) => c[0]), PHASES);
  assert.deepEqual(chips.slice(0, 3).map((c) => c[1]), ["up", "up", "up"]);
  assert.deepEqual(chips[3], ["build", "pending", "Build, running"]);
  assert.deepEqual(chips.slice(4).map((c) => c[1]), ["unavailable", "unavailable", "unavailable"]);
  for (const [, , word] of chips) assert.ok(word.length > 0);
});

test("a finished update shows every phase done and a failure sits on the phase it stopped in", () => {
  assert.ok(phaseChips({ phase: "done" }).every((c) => c[1] === "up"));
  const failed = phaseChips({ phase: "failed", failed_in: "validate" });
  assert.deepEqual(failed[4], ["validate", "down", "Validate failed"]);
  assert.deepEqual(failed.slice(0, 4).map((c) => c[1]), ["up", "up", "up", "up"]);
  assert.equal(failed[5][1], "unavailable");
  // Without failed_in nothing is shown as done.
  const unknown = phaseChips({ phase: "failed" });
  assert.deepEqual(unknown[0], ["received", "down", "Received failed"]);
  assert.ok(unknown.slice(1).every((c) => c[1] === "unavailable"));
  assert.ok(phaseChips(null).every((c) => c[1] === "unavailable"));
});

test("the overall chip follows the request and the state", () => {
  assert.deepEqual(overallChip(null), ["pending", "No update running"]);
  assert.deepEqual(overallChip({ request: { phase: "requested" } }), ["warn", "Requested, waiting for the host helper"]);
  assert.deepEqual(overallChip({ request: { phase: "build" }, state: { phase: "build" } }), ["warn", "Running: Build"]);
  assert.deepEqual(overallChip({ request: null, state: { phase: "done" } }), ["up", "Last update done"]);
  assert.deepEqual(overallChip({ request: null, state: { phase: "failed" } }), ["down", "Failed"]);
  assert.deepEqual(overallChip({ request: null, state: { phase: "fetch", stale: true } }), ["stale", "Stopped without a result"]);
  assert.equal(polling({ request: { phase: "build" } }), true);
  assert.equal(polling({ request: null }), false);
});

test("the upstream line says off, could not check, or the newest commit and tag", () => {
  assert.equal(upstreamText({ enabled: false }, "abc"), "Upstream check is off (server.update_check).");
  assert.equal(upstreamText({ enabled: true, ok: false }, "abc"), "Could not check github.com for a newer version.");
  const gh = { enabled: true, ok: true, latest_commit: "9f2c1d0e8b7a6c5d", latest_tag: "v2026.9.23.6" };
  assert.equal(upstreamText(gh, "2daaa87a1b"), "Newest commit on origin/main: 9f2c1d0, latest release v2026.9.23.6");
  assert.equal(upstreamText({ ...gh, latest_tag: "" }, "9f2c1d0e8b7a6c5d"), "Newest commit on origin/main: 9f2c1d0. This is what is running.");
});

test("a new served version is detected against the one the page loaded with", () => {
  assert.equal(versionChanged("2026.9.23.6", "2026.9.23.6"), false);
  assert.equal(versionChanged("2026.9.23.6", "2026.10.1.0"), true);
  assert.equal(versionChanged("", "2026.10.1.0"), false);
});

test("an agent.update result reads old and new version from the daemon's JSON", () => {
  const out = JSON.stringify({ old_image_id: "sha256:aa", new_image_id: "sha256:bb", old_version: "1.4.0", new_version: "1.5.0" });
  assert.equal(updateResultText(out), "1.4.0 to 1.5.0");
  assert.equal(updateResultText(JSON.stringify({ old_version: "1.5.0", new_version: "1.5.0" })), "already at 1.5.0");
  assert.equal(updateResultText("docker: pull failed"), "docker: pull failed");
  assert.equal(updateResultText("{not json"), "{not json");
  assert.equal(updateResultText(""), "");
  assert.equal(updateResultText("[1]"), "[1]");
});

test("agent rows offer the update, the install command or a reason", () => {
  assert.deepEqual(agentRowState({ eligible: true, reason: "" }), { kind: "update", text: "" });
  assert.deepEqual(agentRowState({ eligible: false, reason: "install command only" }), { kind: "install", text: "install command only" });
  assert.deepEqual(agentRowState({ eligible: false, reason: "no control daemon" }), { kind: "none", text: "no control daemon" });
  assert.deepEqual(agentRowState(undefined), { kind: "none", text: "no data" });
});

test("the update all summary counts and names the refusals", () => {
  const got = updateAllSummary({ queued: [{ host: "a" }, { host: "b" }], refused: [{ host: "c", reason: "no control daemon" }] });
  assert.equal(got.head, "2 queued, 1 refused.");
  assert.deepEqual(got.lines, ["c: no control daemon"]);
  assert.equal(updateAllSummary(null).head, "0 queued, 0 refused.");
});

test("pull ages read as seconds, minutes, hours or days", () => {
  assert.equal(pullAgeText(null), "never");
  assert.equal(pullAgeText(7), "7 s ago");
  assert.equal(pullAgeText(600), "10 min ago");
  assert.equal(pullAgeText(7200), "2 h ago");
  assert.equal(pullAgeText(3 * 86400), "3 d ago");
});

test("Update all says why it is not offered", async () => {
  const { updateAllNote } = await import("../../observe/static/js/updates-logic.js");
  assert.equal(updateAllNote(false, 3), "needs the control plugin");
  assert.match(updateAllNote(true, 0), /^no host can take an agent update/);
  assert.equal(updateAllNote(true, 2), "");
});
