// Map admin: the queue of unlinked switches and the dependency proposals waiting for a
// decision. Every change is a fetch with the session's CSRF token; the CSP forbids native
// form posts. Every string came from a field report or the config, so it is written with
// textContent only.
import { el, when, api, whoami } from "/static/infra-common.js";
import { statusChip, monoTag } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { confirmDialog } from "/static/js/dialog.js";
import { toast } from "/static/js/toast.js";
import { button, notAdmin, showError } from "/static/js/admin-ui.js";

let csrf = "";
const msg = document.getElementById("msg");
const SWITCH_TYPES = ["snmp", "unifi_network", "ping", "tcp"];
let candidates = [];
let tables = null;

async function act(fn, done) {
  msg.textContent = "";
  try { await fn(); if (done) toast(done, "up"); } catch (e) { showError(msg, e.message); }
  await refresh();
}

function decide(kind, e) {
  const accept = kind === "accept";
  return button(accept ? "Accept" : "Reject", accept ? "primary" : "danger", async () => {
    const ok = await confirmDialog({
      title: `${accept ? "Accept" : "Reject"} this dependency?`,
      body: `${e.child} depends on ${e.parent}.`,
      confirmText: accept ? "Accept" : "Reject", danger: !accept });
    if (ok) {
      await act(() => api("POST", `/api/admin/infra/depends/${kind}`, csrf, { child: e.child, parent: e.parent }),
        accept ? "Dependency accepted." : "Dependency rejected.");
    }
  });
}

function linker(sw) {
  const wrap = el("span", "row-actions");
  const sel = el("select");
  sel.setAttribute("aria-label", `Monitor for ${sw.switch_id}`);
  for (const m of candidates) {
    const o = el("option", null, `${m.name} (${m.type})`);
    o.value = m.slug;
    sel.append(o);
  }
  sel.disabled = !candidates.length;
  if (sw.proposed_monitor) sel.value = sw.proposed_monitor;
  const go = button(sw.proposed_monitor ? "Confirm proposed link" : "Link", "primary", () =>
    act(() => api("POST", "/api/admin/infra/link", csrf, { switch_id: sw.switch_id, monitor: sel.value }),
      "Switch linked."));
  go.disabled = !candidates.length;
  wrap.append(sel, go);
  return wrap;
}

function build() {
  const mk = (id, columns, empty, caption) => {
    const t = sortableTable({ columns, rows: [], empty, caption });
    document.getElementById(id).replaceChildren(t.root);
    return t;
  };
  return {
    unlinked: mk("unlinked", [
      { key: "id", label: "Switch", get: (s) => s.switch_id, render: (s) => monoTag(s.switch_id) },
      { key: "name", label: "Name", get: (s) => s.name || "unnamed" },
      { key: "addr", label: "Addresses", get: (s) => s.mgmt_addresses.join(", ") || "none" },
      { key: "seen", label: "Last seen", numeric: true, get: (s) => s.last_seen || 0, render: (s) => when(s.last_seen) },
      { key: "link", label: "Link to monitor", render: linker },
    ], "Every switch seen in the field is linked to a monitor.", "Unlinked switches"),
    pending: mk("pending", [
      { key: "child", label: "Depends", get: (e) => e.child },
      { key: "parent", label: "On", get: (e) => e.parent },
      { key: "source", label: "Source", get: (e) => e.source },
      { key: "seen", label: "Seen", numeric: true, get: (e) => e.last_seen || 0, render: (e) => when(e.last_seen) },
      { key: "act", label: "Decision", render: (e) => {
        const box = el("span", "row-actions");
        box.append(decide("accept", e), decide("reject", e));
        return box;
      } },
    ], "No proposals are waiting for a decision.", "Pending dependency proposals"),
    decided: mk("decided", [
      { key: "child", label: "Depends", get: (r) => r.child },
      { key: "parent", label: "On", get: (r) => r.parent },
      { key: "outcome", label: "Outcome", get: (r) => r.outcome,
        render: (r) => statusChip(r.outcome === "rejected" ? "pending" : "down", r.outcome === "rejected" ? "Rejected" : "Refused") },
      { key: "reason", label: "Reason", get: (r) => r.reason },
      { key: "act", label: "Actions", render: (r) => (r.outcome === "rejected" ? decide("accept", r) : "") },
    ], "Nothing has been decided or refused yet.", "Decided and refused"),
  };
}

async function refresh() {
  try {
    const [unlinked, deps, mons] = await Promise.all([
      api("GET", "/api/admin/infra/unlinked"), api("GET", "/api/infra/dependencies"),
      api("GET", "/api/v2/monitors?limit=500"),
    ]);
    candidates = mons.items.filter((m) => SWITCH_TYPES.includes(m.type));
    if (!tables) tables = build();
    tables.unlinked.setRows(unlinked);
    tables.pending.setRows(deps.pending);
    tables.decided.setRows([
      ...deps.rejected.map((e) => ({ ...e, outcome: "rejected", reason: "decided by an admin" })),
      ...deps.refused.map((e) => ({ ...e, outcome: "refused", reason: e.reason || "" })),
    ]);
  } catch (e) {
    if (e.message !== "not signed in") showError(msg, e.message);
  }
}

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin(document.getElementById("page"), "The map admin page"); return; }
  csrf = me.csrf;
  await refresh();
})();
