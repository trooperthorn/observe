// Retention page: the settings form saves through PUT /api/admin/retention with the CSRF token
// the server put in the page. Messages are written with textContent only.
import { showError } from "/static/js/admin-ui.js";
import { toast } from "/static/js/toast.js";

const form = document.getElementById("retention-form");
const msg = document.getElementById("msg");
const token = () => document.querySelector('meta[name="csrf-token"]').content;

function body() {
  const out = {};
  for (const input of form.querySelectorAll(".retention-fields input")) {
    out[input.name] = Number(input.value);
  }
  const overrides = {};
  for (const row of form.querySelectorAll(".override-row")) {
    const metric = row.querySelector('input[name="metric"]').value.trim();
    if (!metric) continue;
    const levels = {};
    for (const input of row.querySelectorAll('input[type="number"]')) {
      if (input.value !== "") levels[input.name] = Number(input.value);
    }
    overrides[metric] = levels;
  }
  out.overrides = overrides;
  return out;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError(msg, "");
  try {
    const r = await fetch("/api/admin/retention", {
      method: "PUT", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token() },
      body: JSON.stringify(body()),
    });
    if (!r.ok) {
      let detail = `Save failed (${r.status}).`;
      try { detail = (await r.json()).detail || detail; } catch (_) { /* keep the status text */ }
      showError(msg, String(detail));
      return;
    }
    toast("Retention settings saved.", "up");
  } catch (_) {
    showError(msg, "Could not reach the server.");
  }
});
