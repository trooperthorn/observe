// The Add host wizard's rules as pure functions, so they can be tested without a browser. They
// mirror what the server accepts (observe/enrol.py); the server checks everything again.
export const STEPS = ["host", "agent", "allowlist", "install", "live"];

export const NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
export const HEADER_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$/;
export const SERVICE_RE = /^(?:docker:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
export const POOL_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

// What a screen reader is told when the step changes: the step number and its name.
export const STEP_NAMES = {
  host: "Host", agent: "Agent and control", allowlist: "Allowlist", install: "Install", live: "Live progress",
};

export function stepAnnouncement(step) {
  const at = STEPS.indexOf(step);
  return at < 0 ? "" : `Step ${at + 1} of ${STEPS.length}: ${STEP_NAMES[step]}`;
}

export const PLATFORM_LABELS = {
  linux: "Linux server", truenas: "TrueNAS", windows: "Windows", "raspberry-pi": "Raspberry Pi",
};

// Why control cannot be chosen, or "" when it can. Text, so the reason is never only a grey box.
export function controlBlock(platform) {
  if (platform === "windows") {
    return "Control is not available for Windows yet. It needs a Windows path in thermal-control first. The agent can still be installed.";
  }
  if (platform === "truenas") {
    return "Control is not available for TrueNAS yet. The install script sets up the agent only.";
  }
  return "";
}

// Fan headers and services offered by default for a platform. Each is { name, on }.
export function defaultFans(platform) {
  const names = platform === "raspberry-pi" ? ["pwm-fan"] : ["fan1", "fan2", "fan3"];
  return names.map((name) => ({ name, on: platform === "raspberry-pi" || name !== "fan3", limit: "" }));
}

export function defaultServices() {
  return [{ name: "smbd", on: true }, { name: "nfs-server", on: false }];
}

export function platformNote(platform) {
  if (platform === "truenas") {
    return "TrueNAS: the agent runs as an app from a compose file kept on your pool, so it survives updates. The script prints the one step to finish in the TrueNAS Apps screen.";
  }
  if (platform === "windows") return "Windows: run the command in an elevated PowerShell on that machine.";
  if (platform === "raspberry-pi") return "Raspberry Pi: the fan header pwm-fan is selected for you.";
  return "";
}

export function validName(name) {
  return typeof name === "string" && NAME_RE.test(name);
}

export function validHeader(name) {
  return typeof name === "string" && HEADER_RE.test(name);
}

export function validService(name) {
  return typeof name === "string" && SERVICE_RE.test(name) && !name.includes("..");
}

export function validPool(pool) {
  return pool === "" || (POOL_RE.test(pool) && !pool.includes(".."));
}

// "" is no limit; otherwise a whole number 0 to 100. Returns the number, null for none, or
// undefined when the text is not acceptable.
export function parseLimit(text) {
  const t = String(text ?? "").trim();
  if (t === "") return null;
  if (!/^\d{1,3}$/.test(t)) return undefined;
  const n = Number(t);
  return n <= 100 ? n : undefined;
}

// The create request body, or { error } for the first problem found.
export function buildBody(state) {
  if (!validName(state.name)) return { error: "The host name is not valid." };
  if (!PLATFORM_LABELS[state.platform]) return { error: "Choose a platform." };
  const control = !!state.control && !controlBlock(state.platform);
  const body = { name: state.name, platform: state.platform, agent: true, control };
  if (state.platform === "truenas" && state.pool) {
    if (!validPool(state.pool)) return { error: "The pool name is not valid." };
    body.pool = state.pool;
  }
  if (control) {
    const fans = [];
    for (const f of state.fans.filter((x) => x.on)) {
      const limit = parseLimit(f.limit);
      if (limit === undefined) return { error: `The lowest duty for ${f.name} must be a whole number from 0 to 100.` };
      fans.push(limit === null ? f.name : { header: f.name, min_duty_limit: limit });
    }
    body.allowlist = {
      fans, services: state.services.filter((x) => x.on).map((x) => x.name), reboot: !!state.reboot,
      update: state.update !== false,
    };
  }
  return { body };
}

// The step a URL hash asks for, held back to what the person has reached. The command exists
// only in memory, so steps 4 and 5 need a created host.
export function stepFromHash(hash, created) {
  const want = String(hash || "").replace(/^#/, "");
  const i = STEPS.indexOf(want);
  if (i < 0) return "host";
  if (i >= 3 && !created) return "host";
  return STEPS[i];
}

// Progress step status to a chip state and word. Never colour alone: each has an icon and a word.
export const PROGRESS_CHIPS = {
  done: ["up", "Done"],
  waiting: ["pending", "Waiting"],
  skipped: ["pending", "Not chosen"],
  expired: ["down", "Expired"],
};

export function progressChip(status) {
  return PROGRESS_CHIPS[status] || ["pending", "Unknown"];
}

// Install report status to chip state and word.
export const REPORT_CHIPS = {
  ok: ["up", "OK"], skipped: ["pending", "Skipped"], failed: ["down", "Failed"],
  refused: ["down", "Refused"],
};

export function reportChip(status) {
  return REPORT_CHIPS[status] || ["pending", "Unknown"];
}

// The Observe address install commands carry: http(s)://host[:port], no path, never a loopback
// name. The server checks the same rules (observe/config.py normalise_public_url) and is the gate.
export function validPublicUrl(text) {
  const t = String(text ?? "").trim();
  if (!/^https?:\/\/[A-Za-z0-9.:[\]-]+$/.test(t)) return false;
  let u;
  try { u = new URL(t); } catch (_) { return false; }
  const host = u.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host.endsWith(".localhost") || host === "::1" || host === "::") return false;
  if (/^127\./.test(host) || host === "0.0.0.0") return false;
  return true;
}

// What to tell the person about the install command's own state, from a progress reply. Null when
// there is nothing to say: the command is waiting to be run, or the host is ready.
export function noticeFor(progress) {
  if (!progress || progress.ready) return null;
  if (progress.expired) {
    return {
      kind: "expired", title: "Command expired", button: "Regenerate command",
      text: "The install command was not used within its time limit and no longer works. Make a new one; the old one stays dead.",
    };
  }
  if (progress.token_state === "used" && progress.stalled) {
    return {
      kind: "used", title: "Install stopped", button: "Regenerate command",
      text: "This install command was used, but the install has made no progress for 10 minutes or reported a failure. Make a new command: that also revokes the keys the old one made.",
    };
  }
  return null;
}

// Why the script refused to run on the machine, or "" when it has not. The command stays valid.
export function guardText(progress) {
  if (!progress || !progress.guard || !progress.guard.reason) return "";
  return `Refused on the machine: ${progress.guard.reason}.`;
}

// The host page address for a host name.
export function hostHref(name) {
  return `/hosts/${encodeURIComponent(name)}`;
}
