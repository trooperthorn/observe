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

// Copy text to the clipboard. Outside a secure context the readonly field is selected so the
// person can copy it by hand; the toast says which happened.
export async function copyText(text, field) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to the clipboard.", "up");
  } catch (_) {
    if (field) { field.focus(); field.select(); }
    toast("Select the text and copy it by hand.", "warn");
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
