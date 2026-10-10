// The one header summary every page shows (js/shell.js): how many monitors are in each effective
// state, worst first. Page-specific counts (hosts, map devices, one host) stay in the page body.
// Pure, so tests/js/summary.test.mjs runs it without a browser.

export const SUMMARY_ORDER = ["down", "unreachable", "warn", "pending", "up"];

// [[state, count], ...] for the states that have monitors, worst first. A state outside the
// known ones is counted as pending, so a new server state never vanishes from the header.
export function monitorCounts(monitors) {
  const counts = Object.fromEntries(SUMMARY_ORDER.map((s) => [s, 0]));
  for (const m of monitors || []) {
    const s = m && SUMMARY_ORDER.includes(m.effective_state) ? m.effective_state : "pending";
    counts[s] += 1;
  }
  return SUMMARY_ORDER.filter((s) => counts[s]).map((s) => [s, counts[s]]);
}

// The accessible name of the summary, e.g. "Monitors: 2 down, 1 warning, 40 up".
export function summaryLabel(counts, words) {
  if (!counts.length) return "Monitors: none";
  return `Monitors: ${counts.map(([s, n]) => `${n} ${(words[s] || s).toLowerCase()}`).join(", ")}`;
}
