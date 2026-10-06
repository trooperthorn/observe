// The status vocabulary shared by chips and tiles. Pure data, so it can be tested without a
// browser. Each state has a distinct icon shape, so a chip still reads in greyscale, and a
// word, so colour is never the only signal. Roles follow ha_Int_soc (MIT, same owner).
export const STATES = {
  up: { role: "up", icon: "check", word: "Up" },
  ok: { role: "up", icon: "check", word: "OK" },
  warn: { role: "warn", icon: "triangle", word: "Warning" },
  warning: { role: "warn", icon: "triangle", word: "Warning" },
  serious: { role: "serious", icon: "diamond", word: "Soon full" },
  degraded: { role: "degraded", icon: "half", word: "Degraded" },
  down: { role: "down", icon: "cross", word: "Down" },
  critical: { role: "down", icon: "cross", word: "Critical" },
  unreachable: { role: "unreach", icon: "broken", word: "Unreachable" },
  pending: { role: "pending", icon: "hollow", word: "Pending" },
  stale: { role: "pending", icon: "clock", word: "Stale" },
  unavailable: { role: "pending", icon: "hollow", word: "No data" },
  absent: { role: "pending", icon: "hollow", word: "No data" },
  not_reported: { role: "pending", icon: "hollow", word: "No data" },
};

export const FALLBACK = { role: "pending", icon: "hollow", word: "Unknown" };

// Icon drawing instructions: [tag, attributes] pairs on a 16 by 16 grid.
export const ICONS = {
  check: [["circle", { cx: 8, cy: 8, r: 6.5 }], ["path", { d: "M5 8.2l2 2 4-4.4" }]],
  triangle: [["path", { d: "M8 2.2l6.3 11H1.7z" }], ["path", { d: "M8 6.5v3.2M8 11.6v.4" }]],
  diamond: [["path", { d: "M8 1.8l6.2 6.2L8 14.2 1.8 8z" }]],
  cross: [["circle", { cx: 8, cy: 8, r: 6.5 }], ["path", { d: "M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" }]],
  broken: [["path", { d: "M6.5 9.5l-2 2a2.1 2.1 0 0 1-3-3l2-2M9.5 6.5l2-2a2.1 2.1 0 0 1 3 3l-2 2M6 10l1-1M10 6L9 7" }]],
  hollow: [["circle", { cx: 8, cy: 8, r: 6.5 }]],
  half: [["circle", { cx: 8, cy: 8, r: 6.5 }], ["path", { d: "M8 1.5a6.5 6.5 0 0 1 0 13z" }]],
  clock: [["circle", { cx: 8, cy: 8, r: 6.5 }], ["path", { d: "M8 4.6V8l2.4 1.6" }]],
};

export function stateInfo(state) {
  const key = typeof state === "string" ? state.toLowerCase() : "";
  return Object.prototype.hasOwnProperty.call(STATES, key) ? STATES[key] : FALLBACK;
}
