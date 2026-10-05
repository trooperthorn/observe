// Admin screen: users and ingest keys. Every string came from the database or from a host
// agent, so it is written with textContent only. Every change is a fetch with the session's
// CSRF token in X-CSRF-Token; the CSP forbids native form posts. This page offers no action
// that changes a host. The audit log has its own page at /audit.
import { el } from "/static/js/dom.js";
import { api, whoami } from "/static/js/api.js";
import { statusChip, monoTag } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { confirmDialog } from "/static/js/dialog.js";
import { toast } from "/static/js/toast.js";
import { button, copyText, notAdmin, showError } from "/static/js/admin-ui.js";

let csrf = "";
let tables = null;
const msg = document.getElementById("msg");
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "never");

async function run(fn, done) {
  msg.textContent = "";
  try { await fn(); if (done) toast(done, "up"); } catch (e) { showError(msg, e.message); }
  await refresh();
}

function keyColumns() {
  return [
    { key: "id", label: "Id", get: (k) => k.id, render: (k) => monoTag(k.id) },
    { key: "host", label: "Host", get: (k) => k.host },
    { key: "state", label: "State", get: (k) => (k.active ? 0 : 1),
      render: (k) => (k.active ? statusChip("up", "Active") : statusChip("pending", `Revoked ${when(k.revoked_at)}`)) },
    { key: "by", label: "Created by", get: (k) => k.created_by },
    { key: "used", label: "Last used", numeric: true, get: (k) => k.last_used || 0, render: (k) => when(k.last_used) },
    { key: "act", label: "Actions", render: (k) => {
      const box = el("span", "row-actions");
      if (k.active) {
        box.append(button("Revoke", "danger", async () => {
          const ok = await confirmDialog({ title: `Revoke key ${k.id}?`,
            body: `Host ${k.host} will stop being able to report with this key.`,
            confirmText: "Revoke", danger: true });
          if (ok) await run(() => api("POST", `/api/admin/keys/${encodeURIComponent(k.id)}/revoke`, csrf), "Key revoked.");
        }));
      }
      return box;
    } },
  ];
}

function userColumns() {
  return [
    { key: "id", label: "Id", numeric: true, get: (u) => u.id },
    { key: "name", label: "Username", get: (u) => u.username },
    { key: "role", label: "Role", get: (u) => (u.is_admin ? 0 : 1),
      render: (u) => el("span", null, u.is_admin ? "Admin" : "User") },
    { key: "state", label: "State", get: (u) => (u.disabled ? 1 : 0),
      render: (u) => (u.disabled ? statusChip("pending", "Disabled") : statusChip("up", "Enabled")) },
    { key: "act", label: "Actions", render: (u) => {
      const box = el("span", "row-actions");
      box.append(
        button(u.disabled ? "Enable" : "Disable", u.disabled ? "" : "danger", async () => {
          if (!u.disabled) {
            const ok = await confirmDialog({ title: `Disable ${u.username}?`,
              body: "The account will no longer be able to sign in.", confirmText: "Disable", danger: true });
            if (!ok) return;
          }
          await run(() => api("POST", `/api/admin/users/${u.id}/disabled`, csrf, { value: !u.disabled }),
            u.disabled ? "User enabled." : "User disabled.");
        }),
        button(u.is_admin ? "Make user" : "Make admin", "", () =>
          run(() => api("POST", `/api/admin/users/${u.id}/admin`, csrf, { value: !u.is_admin }), "Role changed.")));
      return box;
    } },
  ];
}

function mountTables() {
  const keys = sortableTable({ columns: keyColumns(), rows: [], empty: "No ingest keys yet. Create one above.", caption: "Ingest keys" });
  const users = sortableTable({ columns: userColumns(), rows: [], empty: "No users.", caption: "Users" });
  document.getElementById("keys").replaceChildren(keys.root);
  document.getElementById("users").replaceChildren(users.root);
  return { keys, users };
}

async function refresh() {
  try {
    const [keys, users] = await Promise.all([api("GET", "/api/admin/keys"), api("GET", "/api/admin/users")]);
    if (!tables) tables = mountTables();
    tables.keys.setRows(keys);
    tables.users.setRows(users);
  } catch (e) {
    if (e.message !== "not signed in") showError(msg, e.message);
  }
}

document.getElementById("key-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  msg.textContent = "";
  try {
    const made = await api("POST", "/api/admin/keys", csrf, { host: form.host.value });
    const field = document.getElementById("newkey-value");
    field.value = made.key;
    document.getElementById("newkey").hidden = false;
    field.focus();
    form.reset();
  } catch (e) { showError(msg, e.message); }
  await refresh();
});

document.getElementById("newkey-copy").addEventListener("click", () => {
  const field = document.getElementById("newkey-value");
  copyText(field.value, field);
});

document.getElementById("newkey-dismiss").addEventListener("click", () => {
  document.getElementById("newkey-value").value = "";
  document.getElementById("newkey").hidden = true;
});

document.getElementById("user-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  msg.textContent = "";
  try {
    await api("POST", "/api/admin/users", csrf, {
      username: form.username.value, password: form.password.value, is_admin: form.is_admin.checked,
    });
    form.reset();
    toast("User created.", "up");
  } catch (e) { showError(msg, e.message); }
  await refresh();
});

document.getElementById("logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout", csrf); } catch (_) { /* fall through to the login page */ }
  window.location.assign("/login");
});

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin(document.getElementById("page"), "The admin screen"); return; }
  csrf = me.csrf;
  await refresh();
})();
