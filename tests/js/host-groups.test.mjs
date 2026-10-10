// Run with: node --test tests/js (the tests workflow runs this too).
import test from "node:test";
import assert from "node:assert/strict";
import { groupItems } from "../../observe/static/js/host-groups.js";

const HA = "observe.check.homeassistant";
const upd = (id, value) => ({ source: HA, metric: "observe.ha.update.pending", value, unit: "1",
  labels: { "observe.ha.entity_id": id }, status: value ? "warning" : "good" });
const dom = (d, value) => ({ source: HA, metric: "observe.ha.entity.count", value,
  unit: "{entity}", labels: { "observe.ha.domain": d }, status: "good" });
const total = { source: HA, metric: "observe.ha.entity.count", value: 30, unit: "{entity}",
  labels: {}, status: "good" };
const running = { source: HA, metric: "observe.ha.running", value: 1, unit: "1", labels: {},
  status: "good" };

test("update entities collapse into one row that names the pending ones", () => {
  const items = [running, ...[...Array(182)].map((_, i) => upd(`update.u${i}`, 0)),
    upd("update.phyn_pw1_master_bathroom_firmware_update", 1)];
  const rows = groupItems(items);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].item, running);
  const g = rows[1].group;
  assert.equal(g.summary,
    "1 of 183 updates pending: update.phyn_pw1_master_bathroom_firmware_update");
  assert.equal(g.status, "warning");
  assert.equal(g.items.length, 183);
});

test("many pending updates are named three at a time", () => {
  const rows = groupItems(["a", "b", "c", "d", "e"].map((x) => upd(`update.${x}`, 1)));
  assert.equal(rows[0].group.summary,
    "5 of 5 updates pending: update.a, update.b, update.c and 2 more");
});

test("entity counts per domain are one expandable row; the total stays its own row", () => {
  const rows = groupItems([total, dom("light", 12), dom("sensor", 18)]);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].item, total);
  assert.equal(rows[1].group.summary, "30 entities in 2 domains");
  assert.equal(rows[1].group.items.length, 2);
});

test("a single member and other readings are listed as they are", () => {
  const rows = groupItems([upd("update.only", 0), running]);
  assert.deepEqual(rows.map((r) => !!r.item), [true, true]);
  assert.deepEqual(groupItems([]), []);
});
