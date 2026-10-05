"use strict";
// Sign in with fetch: the CSP sets form-action 'none', so a native form post is blocked.
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
    window.location.assign("/");
  } else {
    msg.textContent = r.status === 429 ? "Too many attempts. Wait a minute." : "Sign in failed.";
  }
});
