// Run in CI with: node --test tests/js
// The v2 client of the console (observe/static/js/api.js): the ETag cache, problem details, the
// single redirect to sign in, the CSRF header, the poller (one run at a time, hidden tab pause,
// backoff, stop) and the shared change loop. Nothing here waits on a real clock: the sleep the
// poller uses is a gate the test opens, and fetch is a fake.
import test from "node:test";
import assert from "node:assert/strict";
import {
  ApiError, api, backoffMs, buildUrl, changes, getAll, get, poller, resetForTests, seconds, whoami,
} from "../../observe/static/js/api.js";

const flush = async (rounds = 6) => { for (let i = 0; i < rounds; i++) await new Promise((r) => setImmediate(r)); };

function res(status, body, headers = {}) {
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    status, ok: status >= 200 && status < 300,
    headers: { get: (k) => (lower[k.toLowerCase()] === undefined ? null : lower[k.toLowerCase()]) },
    json: async () => { if (body === undefined) throw new Error("no body"); return body; },
  };
}

// A fake fetch. `routes` maps a URL prefix to a function (url, init) -> response or a promise.
function installFetch(routes) {
  const calls = [];
  globalThis.fetch = (url, init = {}) => {
    calls.push({ url: String(url), init });
    const key = Object.keys(routes).find((k) => String(url).startsWith(k));
    if (!key) return Promise.reject(new Error(`unexpected fetch ${url}`));
    const out = routes[key](String(url), init, calls.length);
    return new Promise((resolve, reject) => {
      if (init.signal) {
        if (init.signal.aborted) { reject(new Error("aborted")); return; }
        init.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      }
      Promise.resolve(out).then(resolve, reject);
    });
  };
  return calls;
}

// A sleep the test opens by hand, so a poller never waits on a real timer.
function gates() {
  const open = [];
  const sleep = (ms, signal) => new Promise((resolve) => {
    const gate = { ms, resolve };
    open.push(gate);
    if (signal) signal.addEventListener("abort", () => resolve(), { once: true });
  });
  return { sleep, open, release: async () => { const g = open.shift(); if (g) g.resolve(); await flush(); return g; } };
}

function fakeLocation() {
  const seen = [];
  globalThis.location = { pathname: "/audit", search: "?a=1", assign: (u) => seen.push(u) };
  return seen;
}

function fakeDocument(hidden = false) {
  const listeners = new Set();
  const doc = {
    hidden,
    addEventListener: (_t, fn) => listeners.add(fn),
    removeEventListener: (_t, fn) => listeners.delete(fn),
    show() { doc.hidden = false; for (const fn of [...listeners]) fn(); },
  };
  globalThis.document = doc;
  return doc;
}

test.beforeEach(() => {
  resetForTests();
  delete globalThis.document;
  delete globalThis.window;
  delete globalThis.location;
});

test("seconds reads RFC 3339 and unix seconds, buildUrl drops empty values", () => {
  assert.equal(seconds(null), null);
  assert.equal(seconds(12), 12);
  assert.equal(seconds("1970-01-01T00:01:00Z"), 60);
  assert.equal(buildUrl("/a", { x: 1, y: "", z: null, w: "b c" }), "/a?x=1&w=b+c");
  assert.equal(buildUrl("/a?q=1", { x: 2 }), "/a?q=1&x=2");
  assert.equal(buildUrl("/a", {}), "/a");
});

test("get sends If-None-Match and answers a 304 with the cached body", async () => {
  let n = 0;
  const calls = installFetch({
    "/api/v2/hosts": () => (++n === 1 ? res(200, { items: [1] }, { ETag: '"a"' }) : res(304)),
  });
  assert.deepEqual(await get("/api/v2/hosts", { limit: 5 }), { items: [1] });
  assert.equal(calls[0].init.headers["If-None-Match"], undefined);
  assert.deepEqual(await get("/api/v2/hosts", { limit: 5 }), { items: [1] });
  assert.equal(calls[1].init.headers["If-None-Match"], '"a"');
  assert.equal(calls[1].url, "/api/v2/hosts?limit=5");
});

test("the cache is per URL and a body without an ETag is not cached", async () => {
  const calls = installFetch({ "/x": (url) => res(200, { url }, url.includes("tag") ? { ETag: '"t"' } : {}) });
  await get("/x?plain=1");
  await get("/x?plain=1");
  assert.equal(calls[1].init.headers["If-None-Match"], undefined);
  await get("/x?tag=1");
  await get("/x?other=1");
  assert.equal(calls[3].init.headers["If-None-Match"], undefined);
  await get("/x?tag=1");
  assert.equal(calls[4].init.headers["If-None-Match"], '"t"');
});

test("a changed ETag replaces the cached body", async () => {
  let n = 0;
  installFetch({ "/x": () => res(200, { n: ++n }, { ETag: `"v${n + 1}"` }) });
  assert.equal((await get("/x")).n, 1);
  assert.equal((await get("/x")).n, 2);
});

test("problem details become an ApiError with status, type, detail and Retry-After", async () => {
  installFetch({ "/p": () => res(429, { type: "https-problem/rate-limited", detail: "slow down" }, { "Retry-After": "7" }) });
  await assert.rejects(() => get("/p"), (e) => {
    assert.ok(e instanceof ApiError);
    return e.status === 429 && e.detail === "slow down" && e.message === "slow down"
      && e.type === "https-problem/rate-limited" && e.retryAfter === 7;
  });
  installFetch({ "/q": () => res(422, { detail: "bad value", code: "public_url_required" }) });
  await assert.rejects(() => get("/q"), (e) => e.code === "public_url_required" && e.retryAfter === null);
  installFetch({ "/r": () => res(500) });
  await assert.rejects(() => get("/r"), (e) => e.status === 500 && e.detail === "request failed (500)");
});

test("a 401 sends the visitor to sign in once, and redirect: false stays put", async () => {
  const seen = fakeLocation();
  installFetch({ "/a": () => res(401, { detail: "no session" }) });
  await assert.rejects(() => get("/a"), (e) => e.status === 401);
  await assert.rejects(() => get("/a"), (e) => e.status === 401);
  assert.deepEqual(seen, ["/login?next=%2Faudit%3Fa%3D1"]);
  resetForTests();
  seen.length = 0;
  await assert.rejects(() => get("/a", null, { redirect: false }), (e) => e.status === 401);
  assert.deepEqual(seen, []);
});

test("api sends the CSRF header from the session when the page passes none", async () => {
  const calls = installFetch({
    "/api/v2/session": () => res(200, { username: "root", is_admin: true, csrf: "tok" }),
    "/api/admin/tiers": () => res(200, { ok: true }),
  });
  assert.deepEqual(await api("PUT", "/api/admin/tiers", "", { global: {} }), { ok: true });
  const put = calls.find((c) => c.url === "/api/admin/tiers");
  assert.equal(put.init.headers["X-CSRF-Token"], "tok");
  assert.equal(put.init.headers["Content-Type"], "application/json");
  assert.equal(put.init.body, '{"global":{}}');
  await api("POST", "/api/admin/tiers", "explicit", null);
  assert.equal(calls[calls.length - 1].init.headers["X-CSRF-Token"], "explicit");
  assert.equal(calls[calls.length - 1].init.body, "{}");
  assert.equal(calls.filter((c) => c.url === "/api/v2/session").length, 1, "the session is read once");
});

test("api turns a failed write into an Error with the server's detail and code", async () => {
  installFetch({ "/w": () => res(422, { detail: "raw_days must be from 7 to 90", code: "range" }) });
  await assert.rejects(() => api("PUT", "/w", "t", {}), (e) => e.message === "raw_days must be from 7 to 90" && e.code === "range");
  fakeLocation();
  installFetch({ "/w": () => res(401, { detail: "x" }) });
  await assert.rejects(() => api("POST", "/w", "t", {}), (e) => e.message === "not signed in");
  installFetch({ "/g": () => res(401, { detail: "x" }) });
  await assert.rejects(() => api("GET", "/g"), (e) => e.message === "not signed in");
});

test("getAll follows next_cursor", async () => {
  const calls = installFetch({
    "/l": (url) => (url.includes("cursor=c1")
      ? res(200, { items: [3], next_cursor: null })
      : url.includes("cursor=c0") ? res(200, { items: [2], next_cursor: "c1" }) : res(200, { items: [1], next_cursor: "c0" })),
  });
  assert.deepEqual(await getAll("/l", 2), [1, 2, 3]);
  assert.equal(calls[0].url, "/l?limit=2");
  assert.equal(calls[2].url, "/l?limit=2&cursor=c1");
});

test("whoami is read once and a failure is not remembered", async () => {
  let fail = true;
  const calls = installFetch({ "/api/v2/session": () => (fail ? res(500) : res(200, { username: "u" })) });
  await assert.rejects(() => whoami());
  fail = false;
  const [a, b] = await Promise.all([whoami(), whoami()]);
  assert.equal(a.username, "u");
  assert.equal(a, b);
  assert.equal(calls.length, 2);
});

test("backoff follows Retry-After on 429 and 503 and doubles otherwise, with a cap", () => {
  assert.equal(backoffMs(new ApiError(429, "x", { retryAfter: 7 }), 1), 7000);
  assert.equal(backoffMs(new ApiError(503, "x", { retryAfter: 0 }), 4), 0);
  assert.equal(backoffMs(new ApiError(503, "x", { retryAfter: 9999 }), 1), 60000);
  assert.equal(backoffMs(new ApiError(429, "x"), 1), 2000);
  assert.equal(backoffMs(new Error("x"), 3), 8000);
  assert.equal(backoffMs(new Error("x"), 30), 60000);
});

test("the poller never overlaps runs: the next waits for the last to finish, then for the interval", async () => {
  const g = gates();
  let running = 0, peak = 0, runs = 0;
  let finish = null;
  const p = poller(async () => {
    runs += 1; running += 1; peak = Math.max(peak, running);
    await new Promise((r) => { finish = r; });
    running -= 1;
  }, { interval: 5000, sleep: g.sleep, listen: false });
  await flush();
  assert.equal(runs, 1);
  assert.equal(g.open.length, 0, "no timer runs while a run is in flight");
  finish();
  await flush();
  assert.equal(g.open.length, 1);
  assert.equal(g.open[0].ms, 5000);
  assert.equal(runs, 1, "the interval has not passed");
  await g.release();
  assert.equal(runs, 2);
  finish();
  await flush();
  p.stop();
  assert.equal(peak, 1);
});

test("the poller runs once at once even in a hidden tab, then pauses until it shows", async () => {
  // Round 2 R5: a page opened in a background tab showed "No host has reported" because the
  // first fetch waited for the tab to be visible.
  const doc = fakeDocument(true);
  const g = gates();
  let runs = 0;
  const p = poller(async () => { runs += 1; }, { interval: 1000, sleep: g.sleep, listen: false });
  await flush();
  assert.equal(runs, 1, "the first run does not wait for visibility");
  await g.release();
  assert.equal(runs, 1, "still hidden, so the next run waits");
  doc.show();
  await flush();
  assert.equal(runs, 2);
  doc.hidden = true;
  await g.release();
  assert.equal(runs, 2, "hidden again, so the next run waits");
  doc.show();
  await flush();
  assert.equal(runs, 3);
  p.stop();
});

test("a 429 or 503 makes the poller wait for Retry-After before the next run", async () => {
  const g = gates();
  const errors = [];
  let runs = 0;
  const p = poller(async () => {
    runs += 1;
    if (runs === 1) throw new ApiError(429, "slow", { retryAfter: 12 });
    if (runs === 2) throw new ApiError(503, "busy");
  }, { interval: 1000, sleep: g.sleep, listen: false, errorHandler: (e) => errors.push(e.status) });
  await flush();
  assert.equal(g.open[0].ms, 12000);
  await g.release();
  assert.equal(runs, 2);
  assert.equal(g.open[0].ms, 4000, "no Retry-After: the second failure in a row doubles");
  await g.release();
  assert.equal(runs, 3);
  assert.equal(g.open[0].ms, 1000, "a good run goes back to the interval");
  assert.deepEqual(errors, [429, 503]);
  p.stop();
});

test("stop ends the loop at once, even while it sleeps", async () => {
  const g = gates();
  let runs = 0;
  const p = poller(async () => { runs += 1; }, { interval: 60000, sleep: g.sleep, listen: false });
  await flush();
  assert.equal(g.open.length, 1);
  p.stop();
  await flush();
  assert.equal(runs, 1);
  assert.equal(g.open.length, 1, "no new timer after stop");
});

test("the first run can wait one interval with delay", async () => {
  const g = gates();
  let runs = 0;
  const p = poller(async () => { runs += 1; }, { interval: 1000, delay: 3000, sleep: g.sleep, listen: false });
  await flush();
  assert.equal(runs, 0);
  assert.equal(g.open[0].ms, 3000);
  await g.release();
  assert.equal(runs, 1);
  p.stop();
});

test("a page leave stops the poller", async () => {
  const handlers = [];
  globalThis.window = { addEventListener: (t, fn) => handlers.push([t, fn]) };
  const g = gates();
  let runs = 0;
  poller(async () => { runs += 1; }, { interval: 10, sleep: g.sleep });
  await flush();
  assert.equal(handlers[0][0], "pagehide");
  handlers[0][1]();
  await flush();
  await g.release();
  assert.equal(runs, 1);
});

test("with domains the poller reruns when one of them changes, not for another", async () => {
  const g = gates();
  const pending = [];
  const calls = installFetch({
    "/api/v2/changes": (url) => {
      if (!url.includes("since=")) return res(200, { cursor: "c1", changed: [] });
      return new Promise((resolve) => pending.push(resolve));
    },
  });
  let runs = 0;
  const p = poller(async () => { runs += 1; }, { interval: 100, domains: ["monitors"], sleep: g.sleep, listen: false });
  await flush();
  assert.equal(runs, 1);
  assert.ok(calls[1].url.includes("since=c1") && calls[1].url.includes("wait=25"));
  await g.release();  // the interval passes
  assert.equal(runs, 1, "the interval alone does not run it again");
  pending.shift()(res(200, { cursor: "c2", changed: ["map"] }));
  await flush();
  assert.equal(runs, 1, "another domain does not wake it");
  pending.shift()?.(res(200, { cursor: "c3", changed: ["monitors"] }));
  await flush();
  assert.equal(runs, 2);
  p.stop();
  await flush();
});

test("a poller with domains still runs after maxAge when no notice comes", async () => {
  const g = gates();
  installFetch({ "/api/v2/changes": (url) => (url.includes("since=") ? new Promise(() => {}) : res(200, { cursor: "c", changed: [] })) });
  let runs = 0;
  const p = poller(async () => { runs += 1; }, { interval: 100, maxAge: 1000, domains: ["hosts"], sleep: g.sleep, listen: false });
  await flush();
  await g.release();
  assert.equal(g.open[0].ms, 900, "waits the rest of maxAge after the interval");
  await g.release();
  assert.equal(runs, 2);
  p.stop();
});

test("the change loop is shared, delivers by domain and stops with the last subscriber", async () => {
  const g = gates();
  const waiting = [];
  const calls = installFetch({
    "/api/v2/changes": (url) => (url.includes("since=") ? new Promise((resolve) => waiting.push(resolve)) : res(200, { cursor: "c0", changed: [] })),
  });
  const seenA = [], seenB = [];
  const offA = changes.subscribe(["map"], (d) => seenA.push(d), { sleep: g.sleep });
  const offB = changes.subscribe(null, (d) => seenB.push(d), { sleep: g.sleep });
  await flush();
  assert.equal(calls.filter((c) => !c.url.includes("since=")).length, 1, "one loop for both subscribers");
  waiting.shift()(res(200, { cursor: "c1", changed: ["hosts"] }));
  await flush();
  assert.deepEqual(seenA, []);
  assert.deepEqual(seenB, [["hosts"]]);
  waiting.shift()(res(200, { cursor: "c2", changed: ["map", "ports"] }));
  await flush();
  assert.deepEqual(seenA, [["map", "ports"]]);
  offA();
  offB();
  await flush();
  const before = calls.length;
  await flush();
  assert.equal(calls.length, before, "no request after the last unsubscribe");
});

test("the change loop backs off on a failure and honours Retry-After", async () => {
  const g = gates();
  let n = 0;
  installFetch({
    "/api/v2/changes": () => {
      n += 1;
      if (n === 1) return res(503, { detail: "busy" }, { "Retry-After": "4" });
      return res(200, { cursor: "c", changed: [] });
    },
  });
  const off = changes.subscribe(null, () => {}, { sleep: g.sleep });
  await flush();
  assert.equal(g.open[0].ms, 4000);
  off();
  await flush();
});
