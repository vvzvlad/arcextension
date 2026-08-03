// Curator admin login. Served from /admin/login.js under script-src 'self' (no inline).
// Intercepts the form submit and POSTs the token via fetch so a successful login can
// redirect to the console; the raw token is sent once and never stored.
"use strict";

window.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("error");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    const token = document.getElementById("token").value;
    try {
      const res = await fetch("/admin/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: token }),
      });
      if (res.ok) {
        window.location = "/admin";
        return;
      }
      errorBox.textContent = res.status === 401
        ? "Invalid admin token."
        : "Login failed (" + res.status + ").";
      errorBox.hidden = false;
    } catch (_e) {
      errorBox.textContent = "Network error.";
      errorBox.hidden = false;
    }
  });
});
