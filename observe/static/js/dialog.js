// Native <dialog> confirmations. showModal() gives the focus trap and Esc; focus goes back to
// the element that opened the dialog. Lifted from the reboot flow in host-control.js.
import { el } from "/static/js/dom.js";
import { typedMatches } from "/static/js/dialog-logic.js";

let counter = 0;

function open({ title, body, confirmText, danger, typedName }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const dlg = el("dialog", "dlg");
    const h = el("h3", null, title);
    counter += 1;
    h.id = `dlg-title-${counter}`;
    dlg.setAttribute("aria-labelledby", h.id);
    dlg.append(h);
    if (body) dlg.append(el("p", "muted", body));
    let input = null;
    if (typedName) {
      const label = el("label", "field");
      label.append(el("span", null, `Type the name (${typedName}) to confirm`));
      input = el("input");
      input.type = "text";
      input.autocomplete = "off";
      label.append(input);
      dlg.append(label);
    }
    const cancel = el("button", "btn", "Cancel");
    const ok = el("button", danger ? "btn danger" : "btn primary", confirmText || "Confirm");
    cancel.type = ok.type = "button";
    if (input) {
      ok.disabled = true;
      input.addEventListener("input", () => { ok.disabled = !typedMatches(input.value, typedName); });
    }
    let answer = false;
    cancel.addEventListener("click", () => dlg.close());
    ok.addEventListener("click", () => { answer = true; dlg.close(); });
    dlg.addEventListener("close", () => {
      dlg.remove();
      if (opener && typeof opener.focus === "function") opener.focus();
      resolve(answer);
    });
    const row = el("div", "dlg-actions");
    row.append(cancel, ok);
    dlg.append(row);
    document.body.append(dlg);
    dlg.showModal();
    (input || cancel).focus();
  });
}

export function confirmDialog({ title, body, confirmText, danger }) {
  return open({ title, body, confirmText, danger });
}

export function typedConfirm({ title, name, body, confirmText }) {
  return open({ title, body, confirmText: confirmText || "Confirm", danger: true, typedName: name });
}
