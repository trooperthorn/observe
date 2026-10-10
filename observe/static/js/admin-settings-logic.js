// The admin settings pages as pure functions, so they can be tested without a browser. They turn
// what a form holds (text) into the body each PUT route takes, and read the v2 settings documents
// into rows. The server checks every value again; this only keeps a form from sending nonsense.

// A form value as a number: "" is nothing, anything that is not a finite number is refused.
export function numberOrNull(text) {
  const t = String(text === undefined || text === null ? "" : text).trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n)) throw new Error(`"${t}" is not a number`);
  return n;
}

// ---- polling tiers (PUT /api/admin/tiers) ---------------------------------------------------

// values: tier -> text. An empty global cell resets that tier to its default (null); an empty
// per-host cell means "use the global rate" and is left out. A host with no cell set is dropped.
export function tiersBody(globalValues, hostRows) {
  const glob = {};
  for (const [tier, text] of Object.entries(globalValues)) glob[tier] = numberOrNull(text);
  const hosts = {};
  for (const row of hostRows) {
    const host = String(row.host || "").trim();
    if (!host) continue;
    const set = {};
    for (const [tier, text] of Object.entries(row.values)) {
      const n = numberOrNull(text);
      if (n !== null) set[tier] = n;
    }
    if (Object.keys(set).length) hosts[host] = set;
  }
  return { global: glob, hosts };
}

// The host rows a tiers form starts with: every host that has an override, then every known host
// that has none, so an admin can add one without typing a name.
export function tierHostRows(doc) {
  const overridden = Object.keys(doc.hosts || {});
  const rest = (doc.known_hosts || []).filter((h) => !overridden.includes(h));
  return [...overridden, ...rest].map((host) => ({ host, values: { ...((doc.hosts || {})[host] || {}) } }));
}

// ---- re-check (PUT /api/admin/recheck) ------------------------------------------------------

export function recheckBody(globalValues, rows) {
  const out = {};
  for (const [name, text] of Object.entries(globalValues)) out[name] = numberOrNull(text);
  const overrides = {};
  for (const row of rows) {
    const set = {};
    for (const [name, text] of Object.entries(row.values)) {
      const n = numberOrNull(text);
      if (n !== null) set[name] = n;
    }
    if (Object.keys(set).length) overrides[row.slug] = set;
  }
  out.overrides = overrides;
  return out;
}

// ---- retention (PUT /api/admin/retention) ---------------------------------------------------

// Global values are required numbers; an override row with no metric name is ignored and a level
// left empty keeps the global value.
export function retentionBody(globalValues, rows) {
  const out = {};
  for (const [name, text] of Object.entries(globalValues)) {
    const n = numberOrNull(text);
    if (n === null) throw new Error(`${name} needs a number of days`);
    out[name] = n;
  }
  const overrides = {};
  for (const row of rows) {
    const metric = String(row.metric || "").trim();
    if (!metric) continue;
    const levels = {};
    for (const [name, text] of Object.entries(row.values)) {
      const n = numberOrNull(text);
      if (n !== null) levels[name] = n;
    }
    overrides[metric] = levels;
  }
  out.overrides = overrides;
  return out;
}

// The summary chain, finest first: each level keeps at least as long as the one before it, so
// raw data never outlives its own summary (the server refuses the same, see
// observe/storage/rollups.py order_problems).
export const RETENTION_ORDER = ["raw_days", "rollup_5m_days", "hourly_days", "daily_days"];

// A chain of days, finest first: what `own` sets, and for a level it leaves out the global value
// or, when a finer level keeps longer, that finer level's days.
function liftedChain(glob, own) {
  let below = 0;
  return RETENTION_ORDER.map((name) => {
    const days = name in own ? own[name] : Math.max(glob[name] ?? 0, below);
    below = days;
    return days;
  });
}

// One sentence per pair out of order, for the global levels and each override row. A level an
// override leaves empty follows the levels below it, so an override is reported only where it
// sets a summary shorter than a finer level. `label` names a level.
export function retentionOrderProblems(body, label = (name) => name) {
  const out = [];
  const check = (chain, prefix) => {
    for (let i = 0; i < RETENTION_ORDER.length - 1; i++) {
      if (chain[i] > chain[i + 1]) {
        out.push(`${prefix}${label(RETENTION_ORDER[i + 1])} must keep at least as long as ` +
          `${label(RETENTION_ORDER[i]).toLowerCase()} (${chain[i]} days)`);
      }
    }
  };
  const glob = {};
  for (const name of RETENTION_ORDER) if (typeof body[name] === "number") glob[name] = body[name];
  check(RETENTION_ORDER.map((name) => glob[name]), "");
  const base = Object.fromEntries(RETENTION_ORDER.map((name, i) => [name, liftedChain(glob, {})[i]]));
  for (const [metric, own] of Object.entries(body.overrides || {})) check(liftedChain(base, own), `For ${metric}: `);
  return out;
}

// ---- threshold rules (PUT /api/admin/rules) -------------------------------------------------

export const RULE_KINDS = [
  ["consecutive", "Consecutive: the last X samples all breach"],
  ["ratio", "Ratio: X of the last Y samples breach"],
  ["window", "Window: an aggregate over a time window breaches"],
  ["missing", "Missing data: no samples for a gap, or X of Y empty"],
];
export const RULE_CONDITIONS = [["above", "Above"], ["below", "Below"], ["equal", "Equal to"],
  ["not_equal", "Not equal to"], ["outside", "Outside the range"]];
export const RULE_AGGREGATES = [["avg", "Average"], ["min", "Minimum"], ["max", "Maximum"]];
export const MISSING_POLICIES = [["unknown", "Unknown"], ["breaching", "Breaching"], ["not_breaching", "Not breaching"]];
export const SEVERITIES = [["warning", "Warning"], ["critical", "Critical"]];

// A level typed as "80" or, for an outside rule, as "10, 90".
export function levelOf(text, condition) {
  const t = String(text === undefined || text === null ? "" : text).trim();
  if (t === "") return null;
  if (condition === "outside") {
    const parts = t.split(",").map((p) => p.trim());
    if (parts.length !== 2) throw new Error("an outside rule needs a pair such as 10, 90");
    return parts.map((p) => numberOrNull(p));
  }
  return numberOrNull(t);
}

// A rule object from the add-a-rule form. Only the fields the kind takes are included, because
// the server refuses a field that does not belong to the kind.
export function ruleFromFields(f) {
  const rule = { id: String(f.id || "").trim(), kind: f.kind, metric: String(f.metric || "").trim(),
    host: String(f.host || "").trim(), enabled: f.enabled !== false };
  const clear = numberOrNull(f.clear);
  if (f.kind === "missing") {
    rule.severity = f.severity || "warning";
    const gap = numberOrNull(f.gap);
    if (gap !== null) rule.gap = gap;
    else { rule.x = numberOrNull(f.x); rule.y = numberOrNull(f.y); }
  } else {
    rule.condition = f.condition || "above";
    rule.warn = levelOf(f.warn, rule.condition);
    rule.crit = levelOf(f.crit, rule.condition);
    rule.missing = f.missing || "unknown";
    if (f.kind === "consecutive") rule.x = numberOrNull(f.x);
    else if (f.kind === "ratio") { rule.x = numberOrNull(f.x); rule.y = numberOrNull(f.y); }
    else { rule.window = numberOrNull(f.window); rule.agg = f.agg || "avg"; }
  }
  if (clear !== null) rule.clear = clear;
  return rule;
}

function levelText(v) {
  if (v === null || v === undefined) return "none";
  return Array.isArray(v) ? `${v[0]} to ${v[1]}` : String(v);
}

// One line that says what a saved rule does, in words.
export function ruleSummary(r) {
  const scope = r.host ? `on ${r.host}` : "on every host";
  if (r.kind === "missing") {
    const how = r.gap ? `no sample for ${r.gap} seconds` : `${r.x} of the last ${r.y} polls empty`;
    return `${r.metric} ${scope}: ${how}, ${r.severity}`;
  }
  const levels = `warn ${levelText(r.warn)}, crit ${levelText(r.crit)}`;
  const when = r.kind === "consecutive" ? `${r.x} samples in a row`
    : r.kind === "ratio" ? `${r.x} of the last ${r.y} samples`
    : `${r.agg} over ${r.window} seconds`;
  return `${r.metric} ${scope}: ${r.condition}, ${levels}, ${when}`;
}

// ---- storage status -------------------------------------------------------------------------

export const LEVEL_NAMES = {
  compaction: "Compaction of raw polls", raw: "Trim raw samples", "5m": "Trim 5 minute summaries",
  "1h": "Trim hourly summaries", "1d": "Trim daily summaries",
};

export function levelName(level) {
  return LEVEL_NAMES[level] || String(level);
}

// The backend in words: SQLite, PostgreSQL, or PostgreSQL with TimescaleDB.
export function backendText(status) {
  if (status.backend === "sqlite") return "SQLite";
  return status.timescaledb ? "PostgreSQL with TimescaleDB" : "PostgreSQL";
}

export function rollupText(status) {
  if (status.backend === "sqlite") return "The application keeps the summary levels current as samples arrive.";
  if (status.timescaledb) return "TimescaleDB runs the rollups and the compression.";
  return status.incremental_rollups
    ? "The application keeps the summary levels current as samples arrive."
    : "The rollups run on a schedule.";
}
