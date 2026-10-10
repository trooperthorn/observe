// Run in CI with: node --test tests/js
import test from "node:test";
import assert from "node:assert/strict";
import {
  CHIP, GROUPS, actorText, filterRows, httpCode, outcomeOf, outcomeWord,
} from "../../observe/static/js/audit-logic.js";

const refused = { id: 3, ts: 300, actor: "3cf5e4d7f38f", actor_host: "nas01",
  kind: "plugin_request", status: 200, outcome: "refused", remote: "10.0.0.9", detail: { outcome: "refused" } };
const queued = { id: 2, ts: 200, actor: "root", actor_host: null, kind: "control_requested",
  status: 0, outcome: "ok", remote: "", detail: { host: "nas01", action: "service.restart" } };
const fetched = { id: 1, ts: 100, actor: "", actor_host: null, kind: "enrol_fetched", status: 200,
  outcome: "ok", remote: "10.0.0.7", detail: { host: "newbox" } };
const rows = [refused, queued, fetched];

test("every outcome has a filter and a chip state", () => {
  assert.deepEqual(GROUPS.map(([id]) => id), ["ok", "refused", "failed"]);
  assert.deepEqual(CHIP, { ok: "up", refused: "warn", failed: "down" });
});

test("the chip follows the outcome, not the HTTP code", () => {
  assert.equal(outcomeOf(refused), "refused");
  assert.equal(outcomeWord(refused), "Refused");
  assert.equal(outcomeWord(queued), "OK");
  assert.equal(outcomeOf({ status: 200 }), "failed");
});

test("a missing HTTP code is never shown as 0", () => {
  assert.equal(httpCode(0), "");
  assert.equal(httpCode(null), "");
  assert.equal(httpCode(undefined), "");
  assert.equal(httpCode(403), "HTTP 403");
  assert.equal(httpCode(200), "HTTP 200");
});

test("a key actor shows its host and an empty actor says who it was", () => {
  assert.equal(actorText(refused), "3cf5e4d7f38f (nas01)");
  assert.equal(actorText(queued), "root");
  assert.equal(actorText(fetched), "anonymous from 10.0.0.7 for newbox");
  assert.equal(actorText({ actor: "", remote: "", detail: {} }), "anonymous");
  assert.equal(actorText({ actor: "abcdefabcdef", actor_host: null, detail: {} }), "abcdefabcdef");
});

test("the Refused filter returns a refused command and OK does not", () => {
  assert.deepEqual(filterRows(rows, { outcomes: new Set(["refused"]) }), [refused]);
  assert.deepEqual(filterRows(rows, { outcomes: new Set(["ok"]) }), [queued, fetched]);
  assert.deepEqual(filterRows(rows, { outcomes: new Set() }), []);
});

test("the actor filter matches the host shown beside a key, and kind and time filter too", () => {
  assert.deepEqual(filterRows(rows, { actor: " NAS01 " }), [refused]);
  assert.deepEqual(filterRows(rows, { actor: "anonymous" }), [fetched]);
  assert.deepEqual(filterRows(rows, { kind: "control_requested" }), [queued]);
  assert.deepEqual(filterRows(rows, { since: 150 }), [refused, queued]);
});
