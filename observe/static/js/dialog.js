// Native <dialog> confirmations. showModal() gives the focus trap and Esc; focus goes back to
// the element that opened the dialog. Lifted from the reboot flow in host-control.js.
import { el } from "/static/js/dom.js";
import { typedMatches } from "/static/js/dialog-logic.js";

let counter = 0;

function open({ title, body, lines, confirmText, danger, typedName, typedLabel }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const dlg = el("dialog", "dlg");
    const h = el("h3", null, title);
    counter += 1;
    h.id = `dlg-title-${counter}`;
    dlg.setAttribute("aria-labelledby", h.id);
    dlg.append(h);
    if (body) dlg.append(el("p", "muted", body));
    if (lines && lines.length) {
      const list = el("ul", "dlg-lines");
      for (const line of lines) list.append(el("li", null, line));
      dlg.append(list);
    }
    let input = null;
    if (typedName) {
      const label = el("label", "field");
      label.append(el("span", null, typedLabel || `Type the name (${typedName}) to confirm`));
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

// `lines` is an optional list of short strings drawn as a bulleted list under the body, such as
// the changes about to be saved.
export function confirmDialog({ title, body, lines, confirmText, danger }) {
  return open({ title, body, lines, confirmText, danger });
}

// `typedLabel` replaces the field's label ("Type the name (x) to confirm") when the typed word
// is not a host name, such as the word update on the Updates page.
export function typedConfirm({ title, name, body, lines, confirmText, typedLabel }) {
  return open({ title, body, lines, confirmText: confirmText || "Confirm", danger: true, typedName: name, typedLabel });
}
