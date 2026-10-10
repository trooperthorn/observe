// Run in CI with: node --test tests/js
import test from "node:test";
import assert from "node:assert/strict";
import { activeKeysText, isLastAdmin, keyUsage, scopeText } from "../../observe/static/js/keys-logic.js";

test("an ingest and a control key on one host are both simply active", () => {
  const keys = [
    { id: "a", host: "mediain-svr", scope: "wpi", active: true, last_used: "2026-10-10T10:00:00Z" },
    { id: "b", host: "mediain-svr", scope: "wpc", active: true, last_used: "2026-10-10T10:00:05Z" },
  ];
  const u = keyUsage(keys);
  assert.equal(u.get("a"), "");
  assert.equal(u.get("b"), "");
  assert.equal(scopeText("wpc"), "Control");
});

test("two active ingest keys: the most recently used one is in use, the other is spare", () => {
  const keys = [
    { id: "old", host: "h", scope: "wpi", active: true, last_used: "2026-10-01T00:00:00Z" },
    { id: "new", host: "h", scope: "wpi", active: true, last_used: "2026-10-10T00:00:00Z" },
    { id: "gone", host: "h", scope: "wpi", active: false, last_used: null },
  ];
  const u = keyUsage(keys);
  assert.equal(u.get("new"), "in use");
  assert.match(u.get("old"), /^spare/);
  assert.equal(u.has("gone"), false);
});

test("the settings page names the active keys by scope", () => {
  assert.equal(activeKeysText({ wpi: 1, wpc: 1 }), "1 ingest, 1 control");
  assert.equal(activeKeysText({ wpi: 0, wpc: 0 }), "none");
});

test("the only enabled admin is recognised, so its Disable and Make user are not offered", () => {
  const root = { id: 1, is_admin: true, disabled: false };
  const viewer = { id: 2, is_admin: false, disabled: false };
  const old = { id: 3, is_admin: true, disabled: true };
  assert.equal(isLastAdmin([root, viewer, old], root), true);
  assert.equal(isLastAdmin([root, viewer], viewer), false);
  assert.equal(isLastAdmin([root, { id: 4, is_admin: true, disabled: false }], root), false);
});
