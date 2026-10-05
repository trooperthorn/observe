// Dashboard customisation: reorder and hide tiles with up and down buttons and a Hide checkbox.
// The layout is stored per user on the server (/api/ui/layout/dashboard). A failed load keeps the
// declared order and a failed save keeps the screen as the user left it, with a message.
// Ported in spirit from customize.ts in ha_Int_soc (MIT, same owner). No drag and drop.
import { api, whoami } from "/static/js/api.js";
import { el } from "/static/js/dom.js";
import {
  declaredTiles, effectiveHidden, effectiveOrder, layoutBody, moveTile, toggleHidden,
} from "/static/js/tiles-logic.js";

const URL = "/api/ui/layout/dashboard";
const LABELS = { capacity: "Capacity outlook", findings: "Field findings", events: "Recent state changes" };
let order = [];
let hidden = new Set();
let declared = [];
let editing = false;
let csrf = "";

function say(text, bad) {
  const m = document.getElementById("customize-msg");
  if (!m) return;
  m.textContent = text;
  m.className = bad ? "note error" : "note";
}

export function tileLabel(id) {
  return LABELS[id] || id.replace(/^group:/, "Group ");
}

async function save() {
  try {
    await api("PUT", URL, csrf, layoutBody(order, hidden));
    say("Layout saved.", false);
  } catch (err) {
    say(`Could not save the layout (${err.message}). The screen keeps your changes until you reload.`, true);
  }
}

function controls(id, index) {
  const box = el("div", "tile-ctl");
  const mk = (text, label, delta, disabled) => {
    const b = el("button", "btn ghost", text);
    const dir = delta < 0 ? "up" : "down";
    b.type = "button";
    b.setAttribute("aria-label", `${label}: ${tileLabel(id)}`);
    b.dataset.ctl = `${dir}:${id}`;
    b.disabled = disabled;
    b.addEventListener("click", () => {
      order = moveTile(order, id, delta);
      save();
      applyLayout(`${dir}:${id}`);
    });
    return b;
  };
  box.append(el("span", "tile-name", tileLabel(id)),
    mk("Up", "Move up", -1, index === 0), mk("Down", "Move down", 1, index === order.length - 1));
  const label = el("label", "tile-hide");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = hidden.has(id);
  cb.dataset.ctl = `hide:${id}`;
  cb.setAttribute("aria-label", `Hide ${tileLabel(id)}`);
  cb.addEventListener("change", () => {
    hidden = toggleHidden(hidden, id, cb.checked);
    save();
    applyLayout(`hide:${id}`);
  });
  label.append(cb, " Hide");
  box.append(label);
  return box;
}

// Apply the layout to the tiles currently in the page. Called after every render of the dashboard.
// A tile is a node with data-tile. The tiles are appended to the sections container in order,
// which is the only way to order them without inline styles (the CSP forbids those). The control that
// had focus gets it back, or its neighbour when it is now disabled at the edge of the list.
export function applyLayout(focus) {
  order = effectiveOrder(declared, order);
  hidden = effectiveHidden(declared, hidden);
  for (const node of document.querySelectorAll("[data-tile]")) {
    const id = node.dataset.tile;
    const index = order.indexOf(id);
    node.classList.toggle("tile-off", hidden.has(id));
    node.classList.toggle("tile-hidden", hidden.has(id) && !editing);
    node.classList.toggle("tile-edit", editing);
    for (const old of node.querySelectorAll(":scope > .tile-ctl")) old.remove();
    if (editing) node.append(controls(id, index));
  }
  const area = document.getElementById("sections");
  const byId = new Map([...document.querySelectorAll("[data-tile]")].map((n) => [n.dataset.tile, n]));
  for (const id of order) if (byId.has(id)) area.append(byId.get(id));
  if (!focus) return;
  const find = (key) => document.querySelector(`[data-ctl="${CSS.escape(key)}"]`);
  const flip = focus.replace(/^(up|down):/, (_, d) => (d === "up" ? "down:" : "up:"));
  const target = [find(focus), find(flip)].find((n) => n && !n.disabled);
  if (target) target.focus();
}

export function setDeclared(groupNames) {
  declared = declaredTiles(groupNames);
}

export async function initTiles(rerender) {
  const button = document.getElementById("customize");
  const tools = document.getElementById("customize-tools");
  button.addEventListener("click", () => {
    editing = !editing;
    button.setAttribute("aria-pressed", String(editing));
    button.textContent = editing ? "Done" : "Customize";
    tools.hidden = !editing;
    say(editing ? "Use Up, Down and Hide on each section. Hidden sections keep their data." : "", false);
    applyLayout();
  });
  document.getElementById("customize-reset").addEventListener("click", async () => {
    order = [];
    hidden = new Set();
    try { await api("DELETE", URL, csrf); say("Back to the default layout.", false); }
    catch (err) { say(`Could not reset the saved layout (${err.message}).`, true); }
    applyLayout();
  });
  try {
    csrf = (await whoami()).csrf;
    const got = await api("GET", URL);
    order = Array.isArray(got.order) ? got.order : [];
    hidden = new Set(Array.isArray(got.hidden) ? got.hidden : []);
  } catch (_) {
    order = [];  // the declared order; the page still works
    hidden = new Set();
  }
  if (rerender) rerender();
}
