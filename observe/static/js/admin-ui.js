// Small shared builders for the admin, audit, map admin and port pages. Nodes only, textContent only.
import { el } from "/static/js/dom.js";
import { toast } from "/static/js/toast.js";

// A titled card. `sub` is an optional muted line under the title.
export function card(title, sub, ...kids) {
  const c = el("section", "card");
  c.append(el("h3", null, title));
  if (sub) c.append(el("p", "card-sub", sub));
  c.append(...kids);
  return c;
}

export function button(label, kind, onclick) {
  const b = el("button", kind ? `btn ${kind}` : "btn", label);
  b.type = "button";
  b.addEventListener("click", onclick);
  return b;
}

export const SELECTED_MESSAGE = "Selected, press Ctrl+C to copy.";

// Select all the text of a field or a block (an input, a textarea or a pre) so Ctrl+C copies it.
export function selectText(node) {
  if (typeof node.select === "function") {
    node.select();
    return;
  }
  const range = document.createRange();
  range.selectNodeContents(node);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// Copy text to the clipboard. navigator.clipboard exists only in a secure context, so on plain
// HTTP (or when the browser refuses) the field is selected instead and the toast says to press
// Ctrl+C. The field is the element that shows the text: an input or the command block.
export async function copyText(text, field) {
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      throw new Error("clipboard unavailable");
    }
    await navigator.clipboard.writeText(text);
    toast("Copied to the clipboard.", "up");
  } catch (_) {
    if (field) {
      field.focus();
      selectText(field);
    }
    toast(field ? SELECTED_MESSAGE : "Select the text and copy it by hand.", "warn");
  }
}

// Shows an error under the page title and as a toast.
export function showError(line, message) {
  line.textContent = message;
  if (message) toast(message, "down");
}

export function notAdmin(main, what) {
  const c = el("section", "card notice");
  c.setAttribute("role", "alert");
  c.append(el("h3", null, "Admin account needed"), el("p", null, `${what} needs an admin account.`));
  main.replaceChildren(c);
}

// A number input for a settings form. `value` null leaves it empty.
export function numberInput(name, label, { min, max, step = "any", value = null, placeholder = "", required = false } = {}) {
  const input = el("input");
  input.name = name;
  input.type = "number";
  input.step = String(step);
  if (min !== undefined) input.min = String(min);
  if (max !== undefined) input.max = String(max);
  if (value !== null && value !== undefined) input.value = String(value);
  if (placeholder !== "") input.placeholder = String(placeholder);
  input.required = required;
  input.setAttribute("aria-label", label);
  return input;
}
