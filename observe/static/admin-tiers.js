// Polling tiers page: the global rates and one optional override per host, read from
// GET /api/v2/admin/settings/tiers and saved through PUT /api/admin/tiers. Every string is
// written with textContent only.
import { el } from "/static/js/dom.js";
import { numberInput } from "/static/js/admin-ui.js";
import { settingsPage } from "/static/js/settings-page.js";
import { tierHostRows, tiersBody } from "/static/js/admin-settings-logic.js";

const form = document.getElementById("tiers-form");
const globalBox = document.getElementById("tiers-global");
const hostsBox = document.getElementById("tiers-hosts");
let tiers = [];

function hint(b, now) {
  return `${b.min} to ${b.max} seconds, default ${b.default}, now ${now}`;
}

function renderGlobal(doc) {
  const grid = el("div", "settings-grid");
  for (const name of tiers) {
    const b = doc.bounds[name];
    const input = numberInput(name, b.label, { min: b.min, max: b.max, value: doc.saved[name], placeholder: b.default });
    input.id = `g-${name}`;
    const label = el("label", null, b.label);
    label.htmlFor = input.id;
    grid.append(label, input, el("span", "muted", hint(b, doc.global[name])));
  }
  globalBox.replaceChildren(grid);
}

function renderHosts(doc) {
  const rows = tierHostRows(doc);
  if (!rows.length) {
    hostsBox.replaceChildren(el("p", "muted", "No host has an ingest key yet."));
    return;
  }
  const table = el("table", "data");
  table.id = "tiers-host-table";
  table.append(el("caption", "sr-only", "Per-host polling rates in seconds"));
  const head = el("tr");
  head.append(el("th", null, "Host"));
  for (const name of tiers) head.append(el("th", null, doc.bounds[name].label));
  const thead = el("thead");
  thead.append(head);
  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr", "override-row");
    tr.dataset.host = row.host;
    const th = el("th", null, row.host);
    th.scope = "row";
    tr.append(th);
    for (const name of tiers) {
      const b = doc.bounds[name];
      const td = el("td");
      td.append(numberInput(name, `${b.label} for ${row.host}`, { min: b.min, max: b.max, value: row.values[name], placeholder: doc.global[name] }));
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(thead, body);
  hostsBox.replaceChildren(table);
}

function render(doc) {
  tiers = Object.keys(doc.bounds);
  renderGlobal(doc);
  renderHosts(doc);
}

function collect() {
  const globalValues = {};
  for (const input of globalBox.querySelectorAll("input")) globalValues[input.name] = input.value;
  const hostRows = [];
  for (const tr of hostsBox.querySelectorAll("tr.override-row")) {
    const values = {};
    for (const input of tr.querySelectorAll("input")) values[input.name] = input.value;
    hostRows.push({ host: tr.dataset.host, values });
  }
  return tiersBody(globalValues, hostRows);
}

settingsPage({
  docPath: "/api/v2/admin/settings/tiers", putPath: "/api/admin/tiers", what: "The polling tiers page",
  form, msg: document.getElementById("msg"), render, collect, saved: "Polling rates saved.",
});
