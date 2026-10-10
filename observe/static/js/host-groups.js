// Grouping of the readings of one host page section: pure functions, no DOM, so
// tests/js/host-groups.test.mjs can run them without a browser. host.js turns the rows into
// table rows with textContent only.
//
// A Home Assistant host reports one `observe.ha.update.pending` reading per update entity (about
// 200, nearly all 0) and one `observe.ha.entity.count` per entity domain. Listed one by one they
// bury the readings that matter, so each family becomes one summary row that expands on demand.

const RANK = { no_data: -1, good: 0, warning: 1, critical: 2 };

function worst(items) {
  let top = null;
  for (const i of items) {
    if (i.ignored) continue;
    if (top === null || (RANK[i.status] ?? -1) > (RANK[top] ?? -1)) top = i.status;
  }
  return top || "no_data";
}

function isUpdate(i) {
  return i.metric === "observe.ha.update.pending";
}

function isDomainCount(i) {
  return i.metric === "observe.ha.entity.count" && !!(i.labels && i.labels["observe.ha.domain"]);
}

const FAMILIES = [
  {
    key: "ha-updates", test: isUpdate,
    summary(items) {
      const pending = items.filter((i) => i.value)
        .map((i) => (i.labels || {})["observe.ha.entity_id"] || "");
      const head = `${pending.length} of ${items.length} updates pending`;
      if (!pending.length) return head;
      const shown = pending.slice(0, 3).join(", ");
      const more = pending.length > 3 ? ` and ${pending.length - 3} more` : "";
      return `${head}: ${shown}${more}`;
    },
  },
  {
    key: "ha-domains", test: isDomainCount,
    summary(items) {
      const total = items.reduce((n, i) => n + (typeof i.value === "number" ? i.value : 0), 0);
      return `${total} entities in ${items.length} domains`;
    },
  },
];

// Fewer members than this are listed as they are.
export const MIN_GROUP = 2;

// The rows of a section, in order: {item} for a reading, or {group} for a family of readings
// at the place of its first member: {key, summary, status, items}.
export function groupItems(items) {
  const rows = [];
  const groups = new Map();
  const members = new Map(FAMILIES.map((f) => [f.key, (items || []).filter(f.test)]));
  for (const i of items || []) {
    const fam = FAMILIES.find((f) => f.test(i) && members.get(f.key).length >= MIN_GROUP);
    if (!fam) { rows.push({ item: i }); continue; }
    if (groups.has(fam.key)) continue;
    const all = members.get(fam.key);
    const group = { key: fam.key, summary: fam.summary(all), status: worst(all), items: all };
    groups.set(fam.key, group);
    rows.push({ group });
  }
  return rows;
}
