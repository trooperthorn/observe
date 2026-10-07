// The skeleton every admin settings page shares: sign in, ask for an admin, read the settings
// document from /api/v2, draw it, and save the whole form through the page's PUT route. The
// response of a save is the new document, so the form is drawn again from what the server holds.
import { api, get, whoami } from "/static/js/api.js";
import { toast } from "/static/js/toast.js";
import { notAdmin, showError } from "/static/js/admin-ui.js";

export async function settingsPage({ docPath, putPath, what, form, msg, render, collect, saved }) {
  let me;
  try { me = await whoami(); } catch (_) { return; }
  if (!me.is_admin) { notAdmin(document.getElementById("page"), what); return; }
  try {
    render(await get(docPath));
  } catch (e) {
    if (e.status !== 401) showError(msg, e.message);
    return;
  }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    showError(msg, "");
    let body;
    try { body = collect(); } catch (e) { showError(msg, e.message); return; }
    try {
      const out = await api("PUT", putPath, me.csrf, body);
      toast(saved, "up");
      if (out) render(out);
    } catch (e) { showError(msg, e.message); }
  });
}
