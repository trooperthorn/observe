// Row logic for the Audit page (/audit): pure functions over the items of GET /api/v2/audit, so
// tests/js/audit.test.mjs can run them without a browser. No DOM here; audit.js writes every
// value with textContent only.

// The outcome (observe/audit.py outcome()) drives the chip and the filters, not the HTTP code:
// many rows have no code (0), and a request answered 200 can record a refused command.
export const GROUPS = [["ok", "OK"], ["refused", "Refused"], ["failed", "Failed"]];
export const CHIP = { ok: "up", refused: "warn", failed: "down" };
const WORD = Object.fromEntries(GROUPS);

export function outcomeOf(row) {
  return Object.hasOwn(CHIP, row.outcome) ? row.outcome : "failed";
}

export function outcomeWord(row) {
  return WORD[outcomeOf(row)];
}

// The HTTP status as a secondary detail, or "" when the row has none. Never "0".
export function httpCode(status) {
  return Number.isInteger(status) && status >= 100 && status <= 599 ? `HTTP ${status}` : "";
}

// Who did it. A key actor (a key prefix) gets the host its key is bound to; a row with no
// actor was an unauthenticated request, shown with the peer it came from and the host it named.
export function actorText(row) {
  const actor = String(row.actor || "");
  if (actor) return row.actor_host ? `${actor} (${row.actor_host})` : actor;
  const host = row.detail && typeof row.detail.host === "string" ? row.detail.host : "";
  const parts = [row.remote ? `from ${row.remote}` : "", host ? `for ${host}` : ""].filter(Boolean);
  return ["anonymous", ...parts].join(" ");
}

// Rows that pass the page filters: actor text (matched on what is shown, so a host name finds
// its key's rows), one kind, a set of outcomes and a start time in seconds.
export function filterRows(rows, { actor = "", kind = "", outcomes = null, since = 0 } = {}) {
  const needle = actor.trim().toLowerCase();
  return rows.filter((a) => (!needle || actorText(a).toLowerCase().includes(needle))
    && (!kind || a.kind === kind) && (!outcomes || outcomes.has(outcomeOf(a)))
    && a.ts >= since);
}
