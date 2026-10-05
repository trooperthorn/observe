// Shared helpers for the map, port and map admin pages. Every string came from a field
// report, a switch or a monitor name, so it is written with textContent only, never as markup.
"use strict";

const STATE_WORDS = {
  up: "Up", warn: "Warning", down: "Down", unreachable: "Unreachable", pending: "Pending",
  unknown: "State unknown",
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function stateText(node) {
  const word = STATE_WORDS[node.state] || "State unknown";
  return node.state === "unreachable" && node.blocked_by ? `${word}, behind ${node.blocked_by}` : word;
}

function statePill(node) {
  return el("span", `pill ${node.state || "pending"}`, stateText(node));
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" }) : "never";
}

function portHref(switchId, portKey) {
  return `/port?switch_id=${encodeURIComponent(switchId)}&port=${encodeURIComponent(portKey)}`;
}

// A GET or a state-changing request. A request without a session goes to the login page.
async function api(method, path, csrf, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["X-CSRF-Token"] = csrf || "";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opts);
  if (r.status === 401 || (r.status === 403 && method === "GET")) {
    window.location.assign("/login");
    throw new Error("not signed in");
  }
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  if (!r.ok) {
    throw new Error(data && typeof data.detail === "string" ? data.detail : `request failed (${r.status})`);
  }
  return data;
}

async function whoami() {
  const r = await fetch("/api/session");
  if (!r.ok) { window.location.assign("/login"); throw new Error("not signed in"); }
  return r.json();
}
