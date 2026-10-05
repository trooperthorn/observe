// The dashboard layout rules as pure functions, so they can be tested without a browser.
// Ported from effectiveOrder in ha_Int_soc (MIT, same owner). The server checks the shape of
// every id again (observe/layout.py).
export const FIXED_TILES = ["capacity", "findings", "events"];
export const GROUP_PREFIX = "group:";

export function groupTile(name) {
  return GROUP_PREFIX + name;
}

// The declared order: monitor groups by name, then the fixed cards.
export function declaredTiles(groupNames) {
  return [...[...groupNames].sort().map(groupTile), ...FIXED_TILES];
}

// The order to show. Saved ids that are no longer declared are dropped, repeats are dropped, and
// declared ids that were never saved (new groups) are appended in declared order.
export function effectiveOrder(declared, saved) {
  const known = new Set(declared);
  const out = [];
  for (const id of Array.isArray(saved) ? saved : []) {
    if (known.has(id) && !out.includes(id)) out.push(id);
  }
  for (const id of declared) if (!out.includes(id)) out.push(id);
  return out;
}

// The hidden set, limited to tiles that still exist. Hiding never deletes data, it only stops
// the tile being drawn.
export function effectiveHidden(declared, hidden) {
  const known = new Set(declared);
  return new Set((Array.isArray(hidden) ? hidden : []).filter((id) => known.has(id)));
}

// Move one id up (-1) or down (+1). An id at the edge, or not in the list, changes nothing.
// The input is not changed.
export function moveTile(order, id, delta) {
  const i = order.indexOf(id), j = i + delta;
  if (i < 0 || j < 0 || j >= order.length) return order.slice();
  const out = order.slice();
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}

export function toggleHidden(hidden, id, on) {
  const out = new Set(hidden);
  if (on) out.add(id); else out.delete(id);
  return out;
}

// The request body for a save.
export function layoutBody(order, hidden) {
  return { order: order.slice(), hidden: [...hidden] };
}
