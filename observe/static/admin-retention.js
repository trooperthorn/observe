// Retention page: the days each level keeps and the per-metric overrides, read from
// GET /api/v2/admin/settings/retention and saved through PUT /api/admin/retention. Every string
// is written with textContent only.
import { el } from "/static/js/dom.js";
import { numberInput } from "/static/js/admin-ui.js";
import { settingsPage } from "/static/js/settings-page.js";
import { retentionBody, retentionOrderProblems } from "/static/js/admin-settings-logic.js";

const LABELS = {
  raw_days: "Raw samples", rollup_5m_days: "5 minute summaries", hourly_days: "Hourly summaries",
  daily_days: "Daily summaries", history_days: "Availability history", compress_after_days: "Compress raw chunks after",
};
// Why a level has the bounds it has, beside its input.
const HELP = {
  hourly_days: "at least 90: charts of up to a quarter are drawn from hourly summaries",
};
const EXTRA_ROWS = 3;
const form = document.getElementById("retention-form");
const globalBox = document.getElementById("retention-global");
const overridesBox = document.getElementById("retention-overrides");
const problemsBox = document.getElementById("retention-problems");
let current = null;
let table = null;

const label = (name) => LABELS[name] || name;

function overrideRow(doc, metric, values, index) {
  const tr = el("tr", "override-row");
  const first = el("td");
  const name = el("input");
  name.name = "metric";
  name.type = "text";
  name.maxLength = 64;
  name.value = metric;
  name.setAttribute("aria-label", `Metric name for override ${index}`);
  first.append(name);
  tr.append(first);
  for (const level of doc.override_fields) {
    const b = doc.bounds[level];
    const td = el("td");
    td.append(numberInput(level, `${label(level)} for override ${index}`, { min: b.min, max: b.max, step: 1, value: values[level] }));
    tr.append(td);
  }
  return tr;
}

function render(doc) {
  current = doc;
  const grid = el("div", "settings-grid");
  for (const name of Object.keys(doc.bounds)) {
    if (!(name in doc.settings)) continue;
    const b = doc.bounds[name];
    const input = numberInput(name, label(name), { min: b.min, max: b.max, step: 1, value: doc.settings[name], required: true });
    input.id = `f-${name}`;
    const l = el("label", null, label(name));
    l.htmlFor = input.id;
    const help = HELP[name] ? `; ${HELP[name]}` : "";
    grid.append(l, input, el("span", "muted", `${b.min} to ${b.max}, default ${b.default}${help}`));
  }
  globalBox.replaceChildren(grid);
  renderProblems(doc.problems || []);

  document.getElementById("ov-sub").textContent =
    `At most ${doc.max_overrides} metrics. A row with no metric name is ignored. Leave a level empty ` +
    "to keep the global value, or longer when the override keeps a finer level longer.";
  table = el("table", "data");
  table.id = "overrides";
  table.append(el("caption", "sr-only", "Per-metric retention overrides in days"));
  const head = el("tr");
  head.append(el("th", null, "Metric"));
  for (const level of doc.override_fields) head.append(el("th", null, label(level)));
  const thead = el("thead");
  thead.append(head);
  const body = el("tbody");
  const entries = Object.entries(doc.settings.overrides || {});
  entries.forEach(([metric, values], i) => body.append(overrideRow(doc, metric, values, i + 1)));
  for (let i = 0; i < EXTRA_ROWS; i++) body.append(overrideRow(doc, "", {}, entries.length + i + 1));
  table.append(thead, body);
  overridesBox.replaceChildren(table);
}

// Saved values that keep a level longer than its own summary: compaction keeps the summary as
// long as the level below it until they are saved in order.
function renderProblems(problems) {
  problemsBox.hidden = !problems.length;
  if (!problems.length) { problemsBox.replaceChildren(); return; }
  const list = el("ul");
  for (const text of problems) list.append(el("li", null, text));
  problemsBox.replaceChildren(
    el("strong", null, "The saved levels are out of order."),
    el("p", null, "Each level must keep at least as long as the one before it (raw samples, then 5 minute, " +
      "hourly and daily summaries). Until this is fixed and saved, compaction keeps each summary at least as " +
      "long as the level it summarises, so no data is lost."),
    list);
}

document.getElementById("add-override").addEventListener("click", () => {
  if (!current || !table) return;
  const body = table.querySelector("tbody");
  body.append(overrideRow(current, "", {}, body.children.length + 1));
});

function collect() {
  const globalValues = {};
  for (const input of globalBox.querySelectorAll("input")) globalValues[input.name] = input.value;
  const rows = [];
  for (const tr of overridesBox.querySelectorAll("tr.override-row")) {
    const values = {};
    for (const input of tr.querySelectorAll('input[type="number"]')) values[input.name] = input.value;
    rows.push({ metric: tr.querySelector('input[name="metric"]').value, values });
  }
  const body = retentionBody(globalValues, rows);
  const problems = retentionOrderProblems(body, label);
  if (problems.length) throw new Error(`${problems[0]}. Each level must keep at least as long as the one before it.`);
  return body;
}

settingsPage({
  docPath: "/api/v2/admin/settings/retention", putPath: "/api/admin/retention", what: "The retention page",
  form, msg: document.getElementById("msg"), render, collect, saved: "Retention settings saved.",
});
