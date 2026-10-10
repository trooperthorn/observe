// The host settings page's rules as pure functions, so they can be tested without a browser.
// They mirror what the server accepts (observe/enrol.py); the server checks everything again.
import { parseLimit, validHeader, validService } from "./wizard-logic.js";

// The host name in /hosts/<name>/settings, or "" when the path is not that page.
export function hostFromPath(pathname) {
  const m = /^\/hosts\/([^/]+)\/settings\/?$/.exec(String(pathname || ""));
  if (!m) return "";
  try { return decodeURIComponent(m[1]); } catch (_) { return ""; }
}

export function settingsHref(name) {
  return `/hosts/${encodeURIComponent(name)}/settings`;
}

// The server's allowlist ({ fans: [{ header, min_duty_limit? }], services, reboot, update }) as
// an editable draft. Every saved entry starts ticked; unticking one removes it on save. A host
// saved before agent updates existed has no `update`, which reads as off until it is ticked.
export function draftFromAllowlist(allow) {
  const a = allow || {};
  return {
    fans: (a.fans || []).map((f) => ({
      name: f.header, on: true, limit: f.min_duty_limit == null ? "" : String(f.min_duty_limit),
    })),
    services: (a.services || []).map((name) => ({ name, on: true })),
    reboot: !!a.reboot,
    update: !!a.update,
  };
}

// The request body for a draft, or { error } for the first problem found.
export function allowlistFromDraft(draft) {
  const fans = [];
  for (const f of draft.fans.filter((x) => x.on)) {
    if (!validHeader(f.name)) return { error: `The header ${f.name} has characters that are not allowed.` };
    const limit = parseLimit(f.limit);
    if (limit === undefined) return { error: `The lowest duty for ${f.name} must be a whole number from 0 to 100.` };
    fans.push(limit === null ? { header: f.name } : { header: f.name, min_duty_limit: limit });
  }
  const services = [];
  for (const s of draft.services.filter((x) => x.on)) {
    if (!validService(s.name)) return { error: `The service ${s.name} has characters that are not allowed.` };
    services.push(s.name);
  }
  return { allowlist: { fans, services, reboot: !!draft.reboot, update: !!draft.update } };
}

function limitText(f) {
  return f.min_duty_limit == null ? "no lowest duty" : `lowest duty ${f.min_duty_limit}%`;
}

// A short list of the changes between two allowlists, for the confirm dialog. Empty when equal.
export function diffAllowlist(before, after) {
  const lines = [];
  const b = before || { fans: [], services: [], reboot: false };
  const a = after || { fans: [], services: [], reboot: false };
  const old = new Map((b.fans || []).map((f) => [f.header, f]));
  const next = new Map((a.fans || []).map((f) => [f.header, f]));
  for (const [name, f] of next) {
    if (!old.has(name)) lines.push(`Add fan header ${name} (${limitText(f)})`);
    else if (old.get(name).min_duty_limit !== f.min_duty_limit) {
      lines.push(`Change fan header ${name}: ${limitText(old.get(name))} to ${limitText(f)}`);
    }
  }
  for (const name of old.keys()) if (!next.has(name)) lines.push(`Remove fan header ${name}`);
  const oldS = new Set(b.services || []);
  const newS = new Set(a.services || []);
  for (const s of newS) if (!oldS.has(s)) lines.push(`Add service ${s}`);
  for (const s of oldS) if (!newS.has(s)) lines.push(`Remove service ${s}`);
  if (!!b.reboot !== !!a.reboot) lines.push(a.reboot ? "Allow reboot" : "Do not allow reboot");
  if (!!b.update !== !!a.update) {
    lines.push(a.update ? "Allow agent updates from Observe" : "Do not allow agent updates from Observe");
  }
  return lines;
}

// The allowlist status to a chip state and word. Never colour alone: each has an icon and a word.
export const ALLOWLIST_CHIPS = {
  none: ["pending", "No control"],
  pending: ["pending", "Pending"],
  written: ["warn", "Written, waiting for the next pull"],
  applied: ["up", "Applied"],
};

export function allowlistChip(state) {
  return ALLOWLIST_CHIPS[state] || ["pending", "Unknown"];
}

// A sentence under the status chip that says what to do next.
export function allowlistHelp(status, installed) {
  const state = status && status.state;
  if (state === "pending") {
    return installed
      ? "The host still uses its earlier allowlist. Make the update command and run it on the host."
      : "The install command was not run yet. The saved allowlist is in it.";
  }
  if (state === "written") return "control.toml on the host holds the new list. This turns to Applied after the host's control service next pulls.";
  if (state === "applied") return "The host has pulled commands since it received this allowlist.";
  return "";
}

export const TASK_CHIPS = {
  waiting: ["pending", "Waiting for the command to be run"],
  fetched: ["warn", "Running"],
  done: ["up", "Done"],
  failed: ["down", "Failed"],
  expired: ["down", "Expired"],
};

export function taskChip(state) {
  return TASK_CHIPS[state] || ["pending", "Unknown"];
}

export function taskTitle(kind) {
  return kind === "cleanup" ? "Clean up this machine" : "Update the host's allowlist";
}

// Whether a poll should go on: an update or cleanup that is not finished, an allowlist
// that has not been applied yet while a command exists to apply it, or an install command that
// has not been run yet (so a refusal on the wrong machine shows without a reload).
export function shouldPoll(settings, watchingInstall) {
  if (watchingInstall) return true;
  if (settings && settings.enrolment && settings.enrolment.token_state === "valid") return true;
  const task = settings && settings.task;
  return !!task && (task.state === "waiting" || task.state === "fetched");
}
