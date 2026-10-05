// Map admin: the queue of unlinked switches and the dependency proposals waiting for a
// decision. Every change is a fetch with the session's CSRF token; the CSP forbids native
// form posts. Every string came from a field report or the config, so it is written with
// textContent only.
"use strict";

let csrf = "";
const msg = document.getElementById("msg");
const SWITCH_TYPES = ["snmp", "unifi_network", "ping", "tcp"];

function fill(id, rows) {
  document.querySelector(`#${id} tbody`).replaceChildren(...rows);
}

function row(...cells) {
  const tr = el("tr");
  for (const c of cells) {
    const td = el("td");
    if (c instanceof Node) td.append(c); else td.textContent = c;
    tr.append(td);
  }
  return tr;
}

function act(label, fn) {
  const b = el("button", null, label);
  b.type = "button";
  b.addEventListener("click", async () => {
    msg.textContent = "";
    b.disabled = true;
    try { await fn(); } catch (e) { msg.textContent = e.message; }
    await refresh();
  });
  return b;
}

function decide(kind, e) {
  return act(kind === "accept" ? "Accept" : "Reject",
    () => api("POST", `/api/admin/infra/depends/${kind}`, csrf, { child: e.child, parent: e.parent }));
}

function linker(sw, monitors) {
  const wrap = el("span");
  const sel = el("select");
  sel.setAttribute("aria-label", `Monitor for ${sw.switch_id}`);
  for (const m of monitors) {
    const o = el("option", null, `${m.name} (${m.type})`);
    o.value = m.slug;
    sel.append(o);
  }
  sel.disabled = !monitors.length;
  if (sw.proposed_monitor) sel.value = sw.proposed_monitor;
  wrap.append(sel, " ", act(sw.proposed_monitor ? "Confirm proposed link" : "Link", () => api("POST", "/api/admin/infra/link", csrf,
    { switch_id: sw.switch_id, monitor: sel.value })));
  return wrap;
}

async function refresh() {
  try {
    const [unlinked, deps, mons] = await Promise.all([
      api("GET", "/api/admin/infra/unlinked"), api("GET", "/api/infra/dependencies"),
      api("GET", "/api/monitors"),
    ]);
    const candidates = mons.monitors.filter((m) => SWITCH_TYPES.includes(m.type));
    fill("unlinked", unlinked.map((s) => row(s.switch_id, s.name || "unnamed",
      s.mgmt_addresses.join(", ") || "none", when(s.last_seen), linker(s, candidates))));
    fill("pending", deps.pending.map((e) => {
      const buttons = el("span");
      buttons.append(decide("accept", e), " ", decide("reject", e));
      return row(e.child, e.parent, e.source, when(e.last_seen), buttons);
    }));
    fill("decided", [
      ...deps.rejected.map((e) => row(e.child, e.parent, "rejected", "decided by an admin", decide("accept", e))),
      ...deps.refused.map((e) => row(e.child, e.parent, "refused", e.reason || "", "")),
    ]);
  } catch (e) {
    if (e.message !== "not signed in") msg.textContent = e.message;
  }
}

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { msg.textContent = "This page needs an admin account."; return; }
  csrf = me.csrf;
  await refresh();
})();
