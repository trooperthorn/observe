// The Updates page's rules and the agent.update result reader as pure functions, so they can be
// tested without a browser (tests/js/updates.test.mjs). The server checks everything again.

// The phases the host helper reports, in order (observe/updates.py PHASES).
export const PHASES = ["received", "backup", "fetch", "build", "validate", "restart", "done"];

export const PHASE_WORDS = {
  received: "Received", backup: "Backup", fetch: "Fetch", build: "Build", validate: "Validate",
  restart: "Restart", done: "Done", failed: "Failed",
};

// One chip per phase: [phase, chip state, word]. A phase before the current one is done, the
// current one is pending (or down when the update failed there), later ones have no data yet.
// Never colour alone: each chip carries its word, and the failed phase says "failed".
export function phaseChips(state) {
  const phase = state && state.phase;
  const failed = phase === "failed";
  // A failed state names the phase it stopped in as failed_in; without it the first phase
  // carries the failure, so nothing is ever shown as done that was not.
  const at = failed ? Math.max(0, PHASES.indexOf(state.failed_in || "")) : PHASES.indexOf(phase);
  return PHASES.map((p, i) => {
    if (at < 0) return [p, "unavailable", PHASE_WORDS[p]];
    if (failed && i === at) return [p, "down", `${PHASE_WORDS[p]} failed`];
    if (i < at || (!failed && phase === "done")) return [p, "up", PHASE_WORDS[p]];
    if (i === at) return [p, "pending", `${PHASE_WORDS[p]}, running`];
    return [p, "unavailable", PHASE_WORDS[p]];
  });
}

// The chip for the open request or the state as a whole: [state, word].
export function overallChip(status) {
  const req = status && status.request;
  const state = status && status.state;
  if (state && state.phase === "failed") return ["down", "Failed"];
  if (req) return ["warn", req.phase === "requested" ? "Requested, waiting for the host helper" : `Running: ${PHASE_WORDS[req.phase] || req.phase}`];
  if (state && state.phase === "done") return ["up", "Last update done"];
  if (state && state.stale) return ["stale", "Stopped without a result"];
  return ["pending", "No update running"];
}

// Whether the page should poll fast: a request is open.
export function polling(status) {
  return !!(status && status.request);
}

// What to say about the upstream check. `github` is the status document's github object.
export function upstreamText(github, commit) {
  if (!github || !github.enabled) return "Upstream check is off (server.update_check).";
  if (!github.ok) return "Could not check github.com for a newer version.";
  const same = commit && github.latest_commit && github.latest_commit.startsWith(commit.slice(0, 7));
  const head = `Newest commit on origin/main: ${github.latest_commit.slice(0, 7)}`;
  const tag = github.latest_tag ? `, latest release ${github.latest_tag}` : "";
  return `${head}${tag}${same ? ". This is what is running." : ""}`;
}

// After a restart the page compares the served version with the one it loaded with.
export function versionChanged(loaded, served) {
  return !!loaded && !!served && loaded !== served;
}

// The text for an agent.update result. The daemon posts a JSON object
// {old_image_id, new_image_id, old_version, new_version} as its output (docs/CONTROL.md); any
// other output is shown as it is.
export function updateResultText(output) {
  const text = String(output || "").trim();
  if (!text.startsWith("{")) return text;
  let data;
  try { data = JSON.parse(text); } catch (_) { return text; }
  if (!data || typeof data !== "object") return text;
  const oldV = String(data.old_version || "?");
  const newV = String(data.new_version || "?");
  if (oldV === newV) return `already at ${newV}`;
  return `${oldV} to ${newV}`;
}

// A row of the agents table merged from /api/v2/hosts and /api/v2/updates/agents.
// `agent` may be missing for a host the update resource does not know (it never pushed).
export function agentRowState(agent) {
  if (!agent) return { kind: "none", text: "no data" };
  if (agent.eligible) return { kind: "update", text: "" };
  if (agent.reason === "install command only") return { kind: "install", text: "install command only" };
  if (agent.reason === "agent too old, reinstall from host settings") {
    return { kind: "reinstall", text: "agent too old, reinstall from " };
  }
  return { kind: "none", text: agent.reason || "" };
}

// The summary line after Update all.
export function updateAllSummary(result) {
  const queued = (result && result.queued) ? result.queued.length : 0;
  const refused = (result && result.refused) ? result.refused : [];
  const head = `${queued} queued, ${refused.length} refused.`;
  return { head, lines: refused.map((r) => `${r.host}: ${r.reason}`) };
}

export function pullAgeText(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
