"use strict";
// Sign in with fetch: the CSP sets form-action 'none', so a native form post is blocked.
// Where to go after signing in: only a path on this site, never another origin.
function safeNext() {
  const next = new URLSearchParams(window.location.search).get("next") || "/";
  const local = next.startsWith("/") && !next.startsWith("//") && !next.includes(String.fromCharCode(92));
  return local ? next : "/";
}

document.getElementById("login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const msg = document.getElementById("msg");
  msg.textContent = "";
  const r = await fetch("/api/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username: form.username.value, password: form.password.value}),
  });
  if (r.ok) {
    window.location.assign(safeNext());
  } else if (r.status === 429) {
    msg.textContent = "Too many attempts from this browser. Wait a minute, then try again.";
  } else {
    msg.textContent = "Sign in failed. Check the user name and password. After 5 failed attempts an account is locked for 15 minutes.";
  }
});
