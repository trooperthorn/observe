// Toasts: one polite status region, and a second alert region for errors. Errors stay until
// closed; the rest go after six seconds. Never put a secret in a toast.
import { el } from "/static/js/dom.js";
import { statusIcon } from "/static/js/chips.js";
import { stateInfo } from "/static/js/chip-states.js";

export const DISMISS_MS = 6000;

function region(kind) {
  for (const r of document.querySelectorAll(".toasts")) {
    if (r.dataset.kind === kind) return r;
  }
  const r = el("div", "toasts");
  r.dataset.kind = kind;
  r.setAttribute("role", kind === "error" ? "alert" : "status");
  if (kind !== "error") r.setAttribute("aria-live", "polite");
  document.body.append(r);
  return r;
}

// kind is a chip state name: "up" (done), "warn", "pending" (info) or "down" (error).
export function toast(text, kind) {
  const isError = kind === "down" || kind === "error";
  const info = stateInfo(isError ? "down" : kind || "up");
  const t = el("div", `toast s-${info.role}`);
  t.append(statusIcon(info.icon), el("span", "toast-text", String(text)));
  const remove = () => t.remove();
  if (isError) {
    const b = el("button", "toast-close", "Close");
    b.type = "button";
    b.addEventListener("click", remove);
    t.append(b);
  } else {
    setTimeout(remove, DISMISS_MS);
  }
  region(isError ? "error" : "status").append(t);
  return t;
}
