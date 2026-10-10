// Storage page: which backend holds the data, who runs the rollups, the last run of each
// compaction and rollup level, and the change counters. Read only, from
// GET /api/v2/admin/settings/storage, refreshed by the poller. The connection string is never
// part of that document. Every string is written with textContent only.
import { el } from "/static/js/dom.js";
import { get, poller, seconds, whoami } from "/static/js/api.js";
import { statusChip } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { notAdmin } from "/static/js/admin-ui.js";
import { backendText, levelName, rollupText } from "/static/js/admin-settings-logic.js";
import { formatWhen, refreshedText } from "/static/js/format.js";

const when = (ts) => {
  const s = seconds(ts);
  return formatWhen(s);
};

function drawLevels(levels) {
  const columns = [
    { key: "level", label: "Level", get: (r) => r.level },
    { key: "what", label: "Run", get: (r) => levelName(r.level) },
    { key: "last", label: "Last run", get: (r) => seconds(r.last_run) || 0, render: (r) => el("span", null, when(r.last_run)) },
    { key: "rows", label: "Rows processed", get: (r) => r.last_rows },
    { key: "error", label: "Error", get: (r) => (r.last_error ? 1 : 0),
      render: (r) => (r.last_error ? statusChip("down", String(r.last_error)) : statusChip("up", "None")) },
  ];
  const t = sortableTable({ columns, rows: levels, empty: "No compaction has run yet.", caption: "Compaction and rollup runs" });
  document.getElementById("levels").replaceChildren(t.root);
}

function drawSeqs(seqs) {
  const rows = Object.entries(seqs).map(([domain, count]) => ({ domain, count }));
  const t = sortableTable({
    columns: [{ key: "domain", label: "Domain", get: (r) => r.domain }, { key: "count", label: "Changes", get: (r) => r.count }],
    rows, empty: "No counters.", caption: "Change counters",
  });
  document.getElementById("seqs").replaceChildren(t.root);
}

async function refresh() {
  const status = await get("/api/v2/admin/settings/storage");
  document.getElementById("backend-line").textContent = `Storage backend: ${backendText(status)}`;
  document.getElementById("rollup-line").textContent = rollupText(status);
  drawLevels(status.levels);
  drawSeqs(status.change_seqs);
  document.getElementById("footer").textContent = refreshedText();
}

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin(document.getElementById("page"), "The storage page"); return; }
  poller(refresh, { interval: 30000, errorHandler: () => {
    document.getElementById("footer").textContent = "observe unreachable, retrying";
  } });
})();
