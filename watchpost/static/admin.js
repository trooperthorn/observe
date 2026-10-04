// Admin screen: users, ingest keys and the audit log. Every string came from the
// database or from a host agent, so it is written with textContent only. Every change
// is a fetch with the session's CSRF token in X-CSRF-Token; the CSP forbids native
// form posts. This page offers no action that changes a host.
"use strict";

let csrf = "";
const msg = document.getElementById("msg");

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "never";
}

async function api(method, path, body) {
  const opts = {method, headers: {}};
  if (method !== "GET") {
    opts.headers["X-CSRF-Token"] = csrf;
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opts);
  if (r.status === 401 || (r.status === 403 && method === "GET")) {
    window.location.assign("/login");
    throw new Error("not signed in");
  }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `request failed (${r.status})`;
    throw new Error(detail);
  }
  return data;
}

function button(label, onclick) {
  const b = el("button", label);
  b.type = "button";
  b.addEventListener("click", async () => {
    msg.textContent = "";
    try { await onclick(); } catch (e) { msg.textContent = e.message; }
    await refresh();
  });
  return b;
}

function fill(id, rows) {
  const body = document.querySelector(`#${id} tbody`);
  body.replaceChildren(...rows);
}

async function refresh() {
  const [keys, users, rows] = await Promise.all([
    api("GET", "/api/admin/keys"), api("GET", "/api/admin/users"), api("GET", "/api/audit?limit=50"),
  ]);
  fill("keys", keys.map((k) => {
    const tr = el("tr");
    tr.append(el("td", k.id), el("td", k.host),
      el("td", k.active ? "active" : `revoked ${when(k.revoked_at)}`),
      el("td", k.created_by), el("td", when(k.last_used)));
    const act = el("td");
    if (k.active) act.append(button("Revoke", () => api("POST", `/api/admin/keys/${encodeURIComponent(k.id)}/revoke`)));
    tr.append(act);
    return tr;
  }));
  fill("users", users.map((u) => {
    const tr = el("tr");
    tr.append(el("td", String(u.id)), el("td", u.username),
      el("td", u.is_admin ? "admin" : "user"), el("td", u.disabled ? "disabled" : "enabled"));
    const act = el("td");
    act.append(
      button(u.disabled ? "Enable" : "Disable", () => api("POST", `/api/admin/users/${u.id}/disabled`, {value: !u.disabled})),
      button(u.is_admin ? "Make user" : "Make admin", () => api("POST", `/api/admin/users/${u.id}/admin`, {value: !u.is_admin})));
    tr.append(act);
    return tr;
  }));
  fill("audit", rows.map((a) => {
    const tr = el("tr");
    tr.append(el("td", when(a.ts)), el("td", a.actor), el("td", a.kind), el("td", String(a.status)),
      el("td", JSON.stringify(a.detail)));
    return tr;
  }));
}

document.getElementById("key-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  msg.textContent = "";
  try {
    const made = await api("POST", "/api/admin/keys", {host: form.host.value});
    document.getElementById("newkey-value").textContent = made.key;
    document.getElementById("newkey").hidden = false;
    form.reset();
  } catch (e) { msg.textContent = e.message; }
  await refresh();
});

document.getElementById("newkey-dismiss").addEventListener("click", () => {
  document.getElementById("newkey-value").textContent = "";
  document.getElementById("newkey").hidden = true;
});

document.getElementById("user-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  msg.textContent = "";
  try {
    await api("POST", "/api/admin/users", {
      username: form.username.value, password: form.password.value, is_admin: form.is_admin.checked,
    });
    form.reset();
  } catch (e) { msg.textContent = e.message; }
  await refresh();
});

document.getElementById("logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout"); } catch (e) { /* fall through to the login page */ }
  window.location.assign("/login");
});

(async () => {
  const r = await fetch("/api/session");
  if (!r.ok) { window.location.assign("/login"); return; }
  const me = await r.json();
  if (!me.is_admin) { document.getElementById("msg").textContent = "The admin screen needs an admin account."; return; }
  csrf = me.csrf;
  document.getElementById("who").textContent = me.username;
  await refresh();
})();
