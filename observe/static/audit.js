// Audit page: the admin-only audit log with filters. Every string came from the database, so
// it is written with textContent only. The API is GET /api/v2/audit.
import { el } from "/static/js/dom.js";
import { api, whoami } from "/static/js/api.js";
import { seconds } from "/static/js/v2.js";
import { statusChip, monoTag } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { notAdmin, showError } from "/static/js/admin-ui.js";

const msg = document.getElementById("msg");
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "never");
const GROUPS = [["ok", "OK"], ["refused", "Refused"], ["failed", "Failed"]];
const CHIP = { ok: "up", refused: "warn", failed: "down" };
const active = new Set(GROUPS.map(([id]) => id));
let rows = [];

// Status code to group: below 400 worked, 401 and 403 were refused, everything else failed.
function statusGroup(code) {
  if (code < 400) return "ok";
  if (code === 401 || code === 403) return "refused";
  return "failed";
}

const table = sortableTable({
  columns: [
    { key: "ts", label: "Time", numeric: true, get: (a) => a.ts, render: (a) => when(a.ts) },
    { key: "actor", label: "Actor", get: (a) => a.actor },
    { key: "kind", label: "Kind", get: (a) => a.kind, render: (a) => monoTag(a.kind) },
    { key: "status", label: "Status", get: (a) => a.status,
      render: (a) => statusChip(CHIP[statusGroup(a.status)], String(a.status)) },
    { key: "detail", label: "Detail", render: (a) => el("span", "detail-mono", JSON.stringify(a.detail)) },
  ],
  rows: [], pageSizes: [25, 100], empty: "No audit entries match the filters.", caption: "Audit log",
});
document.getElementById("audit").append(table.root);

function visible() {
  const actor = document.getElementById("f-actor").value.trim().toLowerCase();
  const kind = document.getElementById("f-kind").value;
  const range = Number(document.getElementById("f-range").value);
  const since = range ? Date.now() / 1000 - range : 0;
  return rows.filter((a) => (!actor || String(a.actor).toLowerCase().includes(actor))
    && (!kind || a.kind === kind) && active.has(statusGroup(a.status)) && a.ts >= since);
}

function draw() { table.setRows(visible()); }

function fillKinds() {
  const sel = document.getElementById("f-kind");
  for (const k of [...new Set(rows.map((a) => a.kind))].sort()) {
    const o = el("option", null, k);
    o.value = k;
    sel.append(o);
  }
}

function statusToggles() {
  const box = document.getElementById("f-status");
  for (const [id, label] of GROUPS) {
    const b = el("button", "toggle-chip", label);
    b.type = "button";
    b.setAttribute("aria-pressed", "true");
    b.addEventListener("click", () => {
      if (active.has(id)) active.delete(id); else active.add(id);
      b.setAttribute("aria-pressed", String(active.has(id)));
      draw();
    });
    box.append(b);
  }
}

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin(document.getElementById("page"), "The audit log"); return; }
  statusToggles();
  for (const id of ["f-actor", "f-kind", "f-range"]) document.getElementById(id).addEventListener("input", draw);
  try {
    rows = (await api("GET", "/api/v2/audit?limit=500")).items.map((a) => ({ ...a, ts: seconds(a.ts) }));
    fillKinds();
    draw();
  } catch (e) {
    if (e.message !== "not signed in") showError(msg, e.message);
  }
})();
