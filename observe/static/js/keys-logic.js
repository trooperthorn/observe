// Key and user list rules for /admin as pure functions, so tests/js/keys.test.mjs can run them without a
// browser. A host agent has one ingest key (wpi) and, when control was chosen, one control key
// (wpc); both count as the host's active keys, which is why a controlled host shows two.

export const SCOPE_TEXT = { wpi: "Ingest", wpc: "Control", wpr: "Read token" };

export function scopeText(scope) {
  return SCOPE_TEXT[scope] || scope || "";
}

// For each active key, whether it is the one in use: of the active keys with the same host and
// scope, the one used most recently. Another active key of the same host and scope is spare,
// and the page says so, so an admin can tell which one to revoke. Returns id -> note.
export function keyUsage(keys) {
  const groups = new Map();
  for (const k of keys || []) {
    if (!k.active) continue;
    const id = `${k.host}\u0000${k.scope}`;
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(k);
  }
  const out = new Map();
  for (const list of groups.values()) {
    const used = (k) => (k.last_used ? Date.parse(k.last_used) || Number(k.last_used) || 0 : 0);
    list.sort((a, b) => used(b) - used(a));
    list.forEach((k, i) => {
      if (list.length === 1) out.set(k.id, "");
      else if (i === 0) out.set(k.id, used(k) ? "in use" : "");
      else out.set(k.id, `spare: another ${scopeText(k.scope).toLowerCase()} key for this host is in use`);
    });
  }
  return out;
}

// "1 ingest, 1 control" for the host settings page.
export function activeKeysText(byScope) {
  const parts = Object.entries(byScope || {}).filter(([, n]) => n > 0)
    .map(([scope, n]) => `${n} ${scopeText(scope).toLowerCase()}`);
  return parts.length ? parts.join(", ") : "none";
}

// Whether `user` is the only enabled admin. The server refuses to disable or demote that account
// (auth.set_user_flag), so the page does not offer it.
export function isLastAdmin(users, user) {
  if (!user || !user.is_admin || user.disabled) return false;
  return (users || []).filter((u) => u.is_admin && !u.disabled).length <= 1;
}
