// The console's client for the v2 API (docs/DATA-API-DESIGN.md sections 4 and 5).
//
//   get(path, params)   JSON with a per-URL ETag cache: a 304 answers with the cached body, so a
//                       page that polls pays for a body only when something changed.
//   getAll(path)        every item of a cursor-paged list.
//   api(method, path, csrf, body)
//                       the call every page makes. A GET goes through get(); any other method
//                       sends the CSRF header (taken from the session when the page has none)
//                       and a JSON body.
//   poller(fn, opts)    runs fn after each completion, never on a fixed timer, pauses while the
//                       tab is hidden, stops on page leave, backs off on 429 and 503.
//   changes             one shared long poll on /changes per tab, with subscribers by domain.
//   whoami()            the session, read once per page load.
//
// Problem details (RFC 9457) and the older {detail, code} bodies both become an ApiError. A 401
// sends the visitor to the login page once. Everything is looked up on globalThis when it is
// used, so tests/js/api.test.mjs drives it with a fake fetch and fake timers.

const V2 = "/api/v2";
const CACHE_LIMIT = 200;
const MAX_BACKOFF_MS = 60000;
const BASE_BACKOFF_MS = 2000;
const CHANGES_WAIT_S = 25;

export class ApiError extends Error {
  constructor(status, detail, { type = "", code = "", retryAfter = null } = {}) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.type = type;
    this.code = code;       // a machine-readable reason such as public_url_required
    this.retryAfter = retryAfter;  // seconds, from the Retry-After header
  }
}

// A timestamp from the API is an RFC 3339 string. Older code passes unix seconds, so both work.
export function seconds(ts) {
  if (ts === null || ts === undefined) return null;
  return typeof ts === "number" ? ts : Date.parse(ts) / 1000;
}

export function buildUrl(path, params) {
  if (!params) return path;
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue;
    q.set(k, String(v));
  }
  const text = q.toString();
  if (!text) return path;
  return `${path}${path.includes("?") ? "&" : "?"}${text}`;
}

// ---- errors and sign-in ----------------------------------------------------------------------

let redirected = false;

export function redirectToLogin() {
  if (redirected) return;
  redirected = true;
  const loc = globalThis.location || (globalThis.window && globalThis.window.location);
  if (!loc) return;
  const back = (loc.pathname || "/") + (loc.search || "");
  loc.assign(`/login?next=${encodeURIComponent(back)}`);
}

export function resetForTests() {
  redirected = false;
  etags.clear();
  sessionPromise = null;
  stopChanges();
}

async function problemOf(r) {
  let body = null;
  try { body = await r.json(); } catch (_) { body = null; }
  const detail = body && typeof body.detail === "string" ? body.detail : `request failed (${r.status})`;
  const header = r.headers && typeof r.headers.get === "function" ? r.headers.get("Retry-After") : null;
  const wait = header === null || header === undefined ? NaN : Number(header);
  return new ApiError(r.status, detail, {
    type: body && typeof body.type === "string" ? body.type : "",
    code: body && typeof body.code === "string" ? body.code : "",
    retryAfter: Number.isFinite(wait) ? wait : null,
  });
}

async function failure(r, redirect = true) {
  if (r.status === 401 && redirect) redirectToLogin();
  return problemOf(r);
}

// ---- reads ---------------------------------------------------------------------------------

const etags = new Map();  // url -> { etag, body }

function remember(url, etag, body) {
  etags.delete(url);
  etags.set(url, { etag, body });
  while (etags.size > CACHE_LIMIT) etags.delete(etags.keys().next().value);
}

// The parsed body is shared between calls that get a 304, so a caller treats it as read only.
// A page that must stay put when there is no session (the open dashboard) passes redirect: false.
export async function get(path, params, { signal, redirect = true } = {}) {
  const url = buildUrl(path, params);
  const headers = { Accept: "application/json" };
  const held = etags.get(url);
  if (held) headers["If-None-Match"] = held.etag;
  const r = await globalThis.fetch(url, { headers, signal, credentials: "same-origin" });
  if (r.status === 304 && held) return held.body;
  if (!r.ok) throw await failure(r, redirect);
  const body = await r.json();
  const tag = r.headers && typeof r.headers.get === "function" ? r.headers.get("ETag") : null;
  if (tag) remember(url, tag, body); else etags.delete(url);
  return body;
}

// Every item of a list, 500 at a time.
export async function getAll(path, limit = 500, params = {}) {
  const items = [];
  let cursor = null;
  do {
    const page = await get(path, { ...params, limit, cursor });
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return items;
}

// ---- the session -----------------------------------------------------------------------------

let sessionPromise = null;

// Who is signed in, with the CSRF token a write needs. Read once per page load; a failed read
// is not remembered.
export function whoami() {
  if (!sessionPromise) {
    sessionPromise = get(`${V2}/session`).catch((err) => {
      sessionPromise = null;
      throw err;
    });
  }
  return sessionPromise;
}

export function forgetSession() { sessionPromise = null; }

// ---- calls from the pages --------------------------------------------------------------------

export async function api(method, path, csrf, body) {
  if (method === "GET") {
    try { return await get(path); } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw new Error("not signed in");
      throw err;
    }
  }
  const token = csrf || (await whoami()).csrf || "";
  const r = await globalThis.fetch(path, {
    method, credentials: "same-origin",
    headers: { "X-CSRF-Token": token, "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    const err = await failure(r);
    if (r.status === 401) throw new Error("not signed in");
    throw err;
  }
  try { return await r.json(); } catch (_) { return null; }
}

// ---- time and visibility ---------------------------------------------------------------------

function defaultSleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal && signal.aborted) { resolve(); return; }
    const done = () => {
      globalThis.clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = globalThis.setTimeout(done, ms);
    if (signal) signal.addEventListener("abort", done, { once: true });
  });
}

function isHidden() {
  return !!(globalThis.document && globalThis.document.hidden);
}

// Resolves when the tab is visible again (at once when it is visible now, or on abort).
function untilVisible(signal) {
  if (!isHidden()) return Promise.resolve();
  return new Promise((resolve) => {
    const doc = globalThis.document;
    const done = () => {
      doc.removeEventListener("visibilitychange", check);
      if (signal) signal.removeEventListener("abort", done);
      resolve();
    };
    const check = () => { if (!doc.hidden) done(); };
    doc.addEventListener("visibilitychange", check);
    if (signal) signal.addEventListener("abort", done, { once: true });
  });
}

// How long to wait after a failure: Retry-After when the server gave one, else a doubling delay.
export function backoffMs(err, failures) {
  if (err instanceof ApiError && (err.status === 429 || err.status === 503) && err.retryAfter !== null) {
    return Math.min(MAX_BACKOFF_MS, Math.max(0, err.retryAfter * 1000));
  }
  return Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** Math.max(0, failures - 1));
}

// ---- the shared change loop ------------------------------------------------------------------

const subscribers = new Set();  // { domains: Set | null, notify }
let changesCtl = null;

function deliver(changed) {
  for (const s of [...subscribers]) {
    if (s.domains === null || changed.some((d) => s.domains.has(d))) s.notify(changed);
  }
}

async function changesLoop(ctl, sleep) {
  let cursor = null;
  let failures = 0;
  while (!ctl.signal.aborted) {
    await untilVisible(ctl.signal);
    if (ctl.signal.aborted) break;
    try {
      const url = buildUrl(`${V2}/changes`, cursor === null ? null : { since: cursor, wait: CHANGES_WAIT_S });
      const r = await globalThis.fetch(url, { headers: { Accept: "application/json" }, signal: ctl.signal,
        credentials: "same-origin" });
      if (!r.ok) throw await failure(r);
      const body = await r.json();
      failures = 0;
      const first = cursor === null;
      cursor = body.cursor;
      if (!first && Array.isArray(body.changed) && body.changed.length) deliver(body.changed);
    } catch (err) {
      if (ctl.signal.aborted) break;
      failures += 1;
      await sleep(backoffMs(err, failures), ctl.signal);
    }
  }
}

function stopChanges() {
  if (changesCtl) changesCtl.abort();
  changesCtl = null;
}

export const changes = {
  // Calls notify(changedDomains) when a domain in `domains` moved (null means any). Returns the
  // function that removes the subscription; the loop stops with the last subscriber.
  subscribe(domains, notify, { sleep = defaultSleep } = {}) {
    const entry = { domains: domains ? new Set(domains) : null, notify };
    subscribers.add(entry);
    if (!changesCtl) {
      changesCtl = new AbortController();
      changesLoop(changesCtl, sleep).catch(() => {});
    }
    return () => {
      subscribers.delete(entry);
      if (!subscribers.size) stopChanges();
    };
  },
};

// ---- the poller ------------------------------------------------------------------------------

// Runs fn({signal}) and waits for it to finish before it plans the next run. `interval` is the
// least time between two runs. With `domains` the next run also waits for one of those change
// domains to move, and runs anyway after `maxAge` so a missed notice never leaves a page stale.
// `delay` is how long to wait before the first run (a page that just loaded its data waits one
// interval). Returns { stop, poke }: poke asks for a run as soon as the interval allows, and has
// no effect on a poller without domains.
export function poller(fn, { interval = 15000, domains = null, maxAge = null, sleep = defaultSleep,
  errorHandler = null, listen = true, delay = 0 } = {}) {
  const ctl = new AbortController();
  const patience = maxAge === null ? interval * 6 : maxAge;
  let dirty = false;
  let wake = null;
  const nudge = () => { dirty = true; if (wake) { const w = wake; wake = null; w(); } };
  let unsubscribe = null;
  if (domains) unsubscribe = changes.subscribe(domains, nudge, { sleep });

  const waitForChange = () => new Promise((resolve) => {
    if (dirty || ctl.signal.aborted) { resolve(); return; }
    wake = resolve;
    ctl.signal.addEventListener("abort", resolve, { once: true });
    sleep(Math.max(0, patience - interval), ctl.signal).then(() => { if (wake === resolve) wake = null; resolve(); });
  });

  async function loop() {
    let failures = 0;
    if (delay > 0) await sleep(delay, ctl.signal);
    while (!ctl.signal.aborted) {
      await untilVisible(ctl.signal);
      if (ctl.signal.aborted) break;
      dirty = false;
      let pause = interval;
      try {
        await fn({ signal: ctl.signal });
        failures = 0;
      } catch (err) {
        if (ctl.signal.aborted) break;
        failures += 1;
        pause = Math.max(interval, backoffMs(err, failures));
        if (errorHandler) errorHandler(err);
      }
      await sleep(pause, ctl.signal);
      if (domains && failures === 0 && !ctl.signal.aborted) await waitForChange();
    }
  }

  const stop = () => {
    ctl.abort();
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
  };
  if (listen && globalThis.window && typeof globalThis.window.addEventListener === "function") {
    globalThis.window.addEventListener("pagehide", stop, { once: true });
  }
  loop().catch(() => {});
  return { stop, poke: nudge };
}
