// Dashboard customisation: reorder and hide tiles with up and down buttons and a Hide checkbox.
// The layout is stored per user on the server (/api/ui/layout/dashboard). A failed load keeps the
// declared order and a failed save keeps the screen as the user left it, with a message.
// Ported in spirit from customize.ts in ha_Int_soc (MIT, same owner). No drag and drop.
import { api } from "/static/js/api.js";
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
let ready = false;  // true only when the saved layout was loaded, so a save can never overwrite it blindly
let shown = [];     // the order on screen: the saved order limited to the declared tiles
let saving = Promise.resolve();

function say(text, bad) {
  const m = document.getElementById("customize-msg");
  if (!m) return;
  m.textContent = text;
  m.className = bad ? "note error" : "note";
}

export function tileLabel(id) {
  return LABELS[id] || id.replace(/^group:/, "Group ");
}

// Saves run one after another and each sends the newest state, so a slow earlier request can
// never land after a later one.
function save() {
  saving = saving.then(async () => {
    try {
      await api("PUT", URL, csrf, layoutBody(order, hidden));
      say("Layout saved.", false);
    } catch (err) {
      say(`Could not save the layout (${err.message}). The screen keeps your changes until you reload.`, true);
    }
  });
  return saving;
}

// Move within the visible order and keep saved ids of tiles that are not declared right now
// (a group with no monitors for the moment) at the end, so their saved place is not lost.
function moved(id, delta) {
  const next = moveTile(shown, id, delta);
  return [...next, ...order.filter((x) => !next.includes(x))];
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
      order = moved(id, delta);
      save();
      applyLayout(`${dir}:${id}`);
    });
    return b;
  };
  box.append(el("span", "tile-name", tileLabel(id)),
    mk("Up", "Move up", -1, index === 0), mk("Down", "Move down", 1, index === shown.length - 1));
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
  // The saved order and hidden set are kept as loaded; only what is shown is limited to declared tiles.
  shown = effectiveOrder(declared, order);
  const live = effectiveHidden(declared, hidden);
  for (const node of document.querySelectorAll("[data-tile]")) {
    const id = node.dataset.tile;
    const index = shown.indexOf(id);
    node.classList.toggle("tile-off", live.has(id));
    node.classList.toggle("tile-hidden", live.has(id) && !editing);
    node.classList.toggle("tile-edit", editing);
    for (const old of node.querySelectorAll(":scope > .tile-ctl")) old.remove();
    if (editing) node.append(controls(id, index));
  }
  const area = document.getElementById("sections");
  const byId = new Map([...document.querySelectorAll("[data-tile]")].map((n) => [n.dataset.tile, n]));
  for (const id of shown) if (byId.has(id)) area.append(byId.get(id));
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
  button.disabled = true;
  button.addEventListener("click", () => {
    if (!ready) return;
    editing = !editing;
    button.setAttribute("aria-pressed", String(editing));
    button.textContent = editing ? "Done" : "Customize";
    tools.hidden = !editing;
    say(editing ? "Use Up, Down and Hide on each section. Hidden sections keep their data." : "", false);
    applyLayout();
  });
  document.getElementById("customize-reset").addEventListener("click", async () => {
    if (!ready || !window.confirm("Reset the dashboard to the default layout?")) return;
    order = [];
    hidden = new Set();
    try { await api("DELETE", URL, csrf); say("Back to the default layout.", false); }
    catch (err) { say(`Could not reset the saved layout (${err.message}).`, true); }
    applyLayout();
  });
  try {
    // Plain fetches: a viewer with basic auth, or an open dashboard, has no session and must stay on this page.
    const s = await fetch("/api/session");
    if (!s.ok) throw new Error("no session");
    csrf = (await s.json()).csrf || "";
    const r = await fetch(URL);
    if (!r.ok) throw new Error(`layout request failed (${r.status})`);
    const got = await r.json();
    order = Array.isArray(got.order) ? got.order : [];
    hidden = new Set(Array.isArray(got.hidden) ? got.hidden : []);
    ready = true;
    button.disabled = false;
  } catch (err) {
    // The declared order is shown and Customize stays off, so nothing can overwrite a saved layout.
    order = [];
    hidden = new Set();
    if (err.message !== "no session") say("Your saved layout could not be loaded, so Customize is off. Reload to try again.", true);
  }
  if (rerender) rerender();
}
