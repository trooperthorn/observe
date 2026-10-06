// Asking an admin to confirm the address hosts use to reach Observe. The install command never
// uses the address the browser reached the console at, because that comes from the request. The
// server keeps `server.public_url`, or the address saved here, and refuses a command without one
// (code public_url_required). Everything written to the page is text.
import { api } from "/static/js/api.js";
import { validPublicUrl } from "/static/js/wizard-logic.js";

export const NEEDS_URL = "public_url_required";

// Show the confirm box and resolve with the saved address once the server accepts one. `parts` is
// { box, form, input, status }. The box is prefilled with the address of this page when that is
// a usable one, as a suggestion the person confirms or edits.
export function askPublicUrl(parts, csrf) {
  const { box, form, input, status } = parts;
  box.hidden = false;
  status.textContent = "";
  if (!input.value && validPublicUrl(window.location.origin)) input.value = window.location.origin;
  input.focus();
  return new Promise((resolve) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!validPublicUrl(text)) {
        status.textContent = "Enter http or https, then ://, then a host name or IP address and an optional :port. A localhost name will not work, because the command runs on another machine.";
        return;
      }
      try {
        const saved = await api("PUT", "/api/enrol/public-url", csrf, { url: text });
        box.hidden = true;
        resolve(saved.url);
      } catch (err) {
        status.textContent = err.message;
      }
    };
  });
}
