// Re-check page: the global window, interval and good-reply count, and one optional override per
// monitor, read from GET /api/v2/admin/settings/recheck and saved through PUT /api/admin/recheck.
// Every string is written with textContent only.
import { el } from "/static/js/dom.js";
import { numberInput } from "/static/js/admin-ui.js";
import { settingsPage } from "/static/js/settings-page.js";
import { recheckBody } from "/static/js/admin-settings-logic.js";

const LABELS = { window: "Re-check window (seconds)", interval: "Re-check interval (seconds)", good: "Good replies in a row" };
const form = document.getElementById("recheck-form");
const globalBox = document.getElementById("recheck-global");
const overridesBox = document.getElementById("recheck-overrides");
const step = (name) => (name === "good" ? 1 : "any");
const label = (name) => LABELS[name] || name;

function render(doc) {
  const names = Object.keys(doc.bounds);
  const grid = el("div", "settings-grid");
  for (const name of names) {
    const b = doc.bounds[name];
    const input = numberInput(name, label(name), { min: b.min, max: b.max, step: step(name), value: doc.saved[name], placeholder: b.default });
    input.id = `f-${name}`;
    const l = el("label", null, label(name));
    l.htmlFor = input.id;
    grid.append(l, input, el("span", "muted", `${b.min} to ${b.max}, default ${b.default}, now ${doc.settings[name]}`));
  }
  globalBox.replaceChildren(grid);

  const table = el("table", "data");
  table.append(el("caption", "sr-only", "Per-monitor re-check overrides"));
  const head = el("tr");
  head.append(el("th", null, "Monitor"));
  for (const name of names) head.append(el("th", null, label(name)));
  const thead = el("thead");
  thead.append(head);
  const body = el("tbody");
  for (const m of doc.monitors) {
    const tr = el("tr", "override-row");
    tr.dataset.slug = m.slug;
    const th = el("th", null, m.name);
    th.scope = "row";
    tr.append(th);
    const own = doc.overrides[m.slug] || {};
    for (const name of names) {
      const b = doc.bounds[name];
      const td = el("td");
      td.append(numberInput(name, `${label(name)} for ${m.name}`, { min: b.min, max: b.max, step: step(name), value: own[name] }));
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(thead, body);
  overridesBox.replaceChildren(doc.monitors.length ? table : el("p", "muted", "No monitors are configured."));
}

function collect() {
  const globalValues = {};
  for (const input of globalBox.querySelectorAll("input")) globalValues[input.name] = input.value;
  const rows = [];
  for (const tr of overridesBox.querySelectorAll("tr.override-row")) {
    const values = {};
    for (const input of tr.querySelectorAll("input")) values[input.name] = input.value;
    rows.push({ slug: tr.dataset.slug, values });
  }
  return recheckBody(globalValues, rows);
}

settingsPage({
  docPath: "/api/v2/admin/settings/recheck", putPath: "/api/admin/recheck", what: "The re-check page",
  form, msg: document.getElementById("msg"), render, collect, saved: "Re-check settings saved.",
});
