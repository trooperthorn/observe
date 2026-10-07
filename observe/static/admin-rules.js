// Threshold rules page: the saved rules from GET /api/v2/admin/settings/rules, an add-a-rule form,
// and one save of the whole list through PUT /api/admin/rules. The list on screen is a draft until
// Save is pressed. Every string is written with textContent only.
import { el } from "/static/js/dom.js";
import { api, get, whoami } from "/static/js/api.js";
import { statusChip } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { toast } from "/static/js/toast.js";
import { button, notAdmin, showError } from "/static/js/admin-ui.js";
import {
  MISSING_POLICIES, RULE_AGGREGATES, RULE_CONDITIONS, RULE_KINDS, SEVERITIES, ruleFromFields, ruleSummary,
} from "/static/js/admin-settings-logic.js";

const msg = document.getElementById("msg");
const listBox = document.getElementById("rules-list");
const countLine = document.getElementById("rules-count");
const fieldsBox = document.getElementById("rule-fields");
const form = document.getElementById("rule-form");
let rules = [];
let maxRules = 500;
let csrf = "";
let dirty = false;
let tableView = null;
const inputs = {};

// Which fields each kind of rule takes. The rest are hidden, so the form never sends a field the
// server would refuse for that kind.
const SHOWS = {
  consecutive: ["condition", "warn", "crit", "missing", "x", "clear"],
  ratio: ["condition", "warn", "crit", "missing", "x", "y", "clear"],
  window: ["condition", "warn", "crit", "missing", "window", "agg", "clear"],
  missing: ["severity", "gap", "x", "y", "clear"],
};

function field(name, label, control) {
  const row = el("label", null, label);
  row.dataset.field = name;
  row.append(control);
  inputs[name] = control;
  fieldsBox.append(row);
}

function textInput(name, max) {
  const i = el("input");
  i.name = name;
  i.type = "text";
  i.maxLength = max;
  return i;
}

function numberBox(name) {
  const i = el("input");
  i.name = name;
  i.type = "number";
  i.step = "any";
  return i;
}

function selectBox(name, options) {
  const s = el("select");
  s.name = name;
  for (const [value, text] of options) {
    const o = el("option", null, text);
    o.value = value;
    s.append(o);
  }
  return s;
}

function buildForm() {
  field("id", "Rule id", textInput("id", 64));
  field("kind", "Kind", selectBox("kind", RULE_KINDS));
  field("metric", "Metric", textInput("metric", 128));
  field("host", "Host (empty for every host)", textInput("host", 128));
  field("condition", "Condition", selectBox("condition", RULE_CONDITIONS));
  field("warn", "Warn level (two numbers, low and high, for an outside rule)", textInput("warn", 64));
  field("crit", "Critical level", textInput("crit", 64));
  field("missing", "A sample with no data counts as", selectBox("missing", MISSING_POLICIES));
  field("x", "X (samples)", numberBox("x"));
  field("y", "Y (samples)", numberBox("y"));
  field("window", "Window (seconds)", numberBox("window"));
  field("agg", "Aggregate", selectBox("agg", RULE_AGGREGATES));
  field("severity", "Severity", selectBox("severity", SEVERITIES));
  field("gap", "No sample for (seconds)", numberBox("gap"));
  field("clear", "Samples that must be fine to clear (empty for X)", numberBox("clear"));
  inputs.kind.addEventListener("change", showFields);
  showFields();
}

function showFields() {
  const shown = new Set(["id", "kind", "metric", "host", ...SHOWS[inputs.kind.value]]);
  for (const row of fieldsBox.querySelectorAll("label")) row.hidden = !shown.has(row.dataset.field);
}

function readFields() {
  const f = {};
  for (const [name, control] of Object.entries(inputs)) {
    if (!fieldsBox.querySelector(`[data-field="${name}"]`).hidden) f[name] = control.value;
  }
  return f;
}

function draw() {
  countLine.textContent = `${rules.length} of at most ${maxRules} rules.${dirty ? " Changes are not saved yet." : ""}`;
  const columns = [
    { key: "id", label: "Id", get: (r) => r.id },
    { key: "rule", label: "Rule", get: (r) => ruleSummary(r) },
    { key: "enabled", label: "State", get: (r) => (r.enabled ? 0 : 1),
      render: (r) => (r.enabled ? statusChip("up", "Enabled") : statusChip("pending", "Disabled")) },
    { key: "act", label: "Actions", render: (r) => {
      const box = el("span", "row-actions");
      box.append(
        button(r.enabled ? "Disable" : "Enable", "", () => { r.enabled = !r.enabled; dirty = true; draw(); }),
        button("Remove", "danger", () => { rules = rules.filter((x) => x !== r); dirty = true; draw(); }));
      return box;
    } },
  ];
  tableView = sortableTable({ columns, rows: rules, empty: "No rules are saved.", caption: "Threshold rules" });
  listBox.replaceChildren(tableView.root);
}

function load(doc) {
  rules = doc.rules.map((r) => ({ ...r }));
  maxRules = doc.max_rules;
  dirty = false;
  draw();
}

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  showError(msg, "");
  try {
    const rule = ruleFromFields(readFields());
    if (rules.some((r) => r.id === rule.id)) throw new Error(`a rule with the id ${rule.id} is already in the list`);
    if (rules.length >= maxRules) throw new Error(`at most ${maxRules} rules are allowed`);
    rules = [...rules, rule];
    dirty = true;
    draw();
    form.reset();
    showFields();
    toast("Added to the list. Press Save rules to keep it.", "up");
  } catch (e) { showError(msg, e.message); }
});

document.getElementById("rules-save").addEventListener("click", async () => {
  showError(msg, "");
  try {
    const out = await api("PUT", "/api/admin/rules", csrf, { rules });
    toast("Rules saved.", "up");
    load(out);
  } catch (e) { showError(msg, e.message); }
});

(async () => {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin(document.getElementById("page"), "The threshold rules page"); return; }
  csrf = me.csrf;
  buildForm();
  try { load(await get("/api/v2/admin/settings/rules")); } catch (e) { if (e.status !== 401) showError(msg, e.message); }
})();
