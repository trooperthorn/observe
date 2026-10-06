// Fetch wrapper for the console API: JSON bodies, the CSRF header on state-changing requests,
// a redirect to the login page when there is no session, and Error messages taken from the
// server's detail field.
export async function api(method, path, csrf, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["X-CSRF-Token"] = csrf || "";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opts);
  if (r.status === 401 || (r.status === 403 && method === "GET")) {
    window.location.assign("/login");
    throw new Error("not signed in");
  }
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  if (!r.ok) {
    const err = new Error(data && typeof data.detail === "string" ? data.detail : `request failed (${r.status})`);
    // A machine-readable reason, such as public_url_required, so a page can ask for what is missing.
    if (data && typeof data.code === "string") err.code = data.code;
    throw err;
  }
  return data;
}

export async function whoami() {
  const r = await fetch("/api/session");
  if (!r.ok) { window.location.assign("/login"); throw new Error("not signed in"); }
  return r.json();
}
