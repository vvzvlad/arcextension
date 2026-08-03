// Curator admin console logic. Served from /admin/app.js under script-src 'self' (there is
// NO inline script). It renders the #35 /admin JSON API through same-origin fetch.
//
// SECURITY (issue #36 acc 6): suggested_title and origin are UNAUTHENTICATED input. They are
// ONLY ever written with element.textContent — this file assigns raw markup to no element —
// so a value like `<img src=x onerror=alert(1)>` renders as literal text, never as markup.
"use strict";

// --- small DOM helpers (textContent only) ------------------------------------
function el(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) {
    node.textContent = String(text); // text node only: untrusted values stay inert markup
  }
  return node;
}

function showError(message) {
  const box = document.getElementById("error");
  box.textContent = String(message);
  box.hidden = false;
}

function clearError() {
  const box = document.getElementById("error");
  box.textContent = "";
  box.hidden = true;
}

// --- fetch wrappers (same-origin, cookie-authenticated) ----------------------
// credentials:'same-origin' sends the session cookie; the browser adds Origin +
// Sec-Fetch-Site on mutating requests, which the server's CSRF gate checks (acc 3).
async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (res.status === 401) {
    window.location = "/admin/login";
    throw new Error("unauthenticated");
  }
  if (!res.ok) throw new Error(path + " -> " + res.status);
  return res.json();
}

async function apiSend(method, path, body) {
  const res = await fetch(path, {
    method: method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    window.location = "/admin/login";
    throw new Error("unauthenticated");
  }
  if (!res.ok) {
    let detail = res.status;
    try {
      const j = await res.json();
      if (j && j.error) detail = j.error;
    } catch (_e) { /* non-JSON error body: keep the status code */ }
    throw new Error(method + " " + path + " -> " + detail);
  }
  return res.json();
}

// --- enrollment window -------------------------------------------------------
async function renderWindow() {
  const status = document.getElementById("window-status");
  const w = await apiGet("/admin/enroll/window");
  // Clear then rebuild via textContent nodes (no raw-markup assignment).
  status.textContent = "";
  if (w.open) {
    status.appendChild(el("span", "OPEN — code "));
    status.appendChild(el("code", w.code));
    status.appendChild(el("span", " — " + w.seconds_remaining + "s remaining"));
  } else {
    status.appendChild(el("span", "closed"));
  }
}

// --- pending enroll requests -------------------------------------------------
async function renderRequests() {
  const body = document.getElementById("requests-body");
  const empty = document.getElementById("requests-empty");
  const data = await apiGet("/admin/enroll/requests");
  body.textContent = "";
  const rows = data.requests || [];
  empty.hidden = rows.length > 0;
  for (const r of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", r.install_uuid_short)); // first-8, server-provided
    tr.appendChild(el("td", r.suggested_title));    // UNTRUSTED -> textContent
    tr.appendChild(el("td", r.origin));             // UNTRUSTED -> textContent
    tr.appendChild(el("td", r.protocol_version));
    tr.appendChild(el("td", r.id_exists ? "yes" : "no"));

    // approve-as input (operator types the instance_id to assign)
    const idCell = el("td");
    const idInput = el("input");
    idInput.type = "text";
    idInput.placeholder = "instance_id";
    idCell.appendChild(idInput);
    tr.appendChild(idCell);

    // approve / reject buttons
    const actionCell = el("td");
    const approveBtn = el("button", "Approve");
    approveBtn.type = "button";
    approveBtn.addEventListener("click", async () => {
      try {
        clearError();
        await apiSend("POST", "/admin/enroll/approve", {
          install_uuid: r.install_uuid,
          instance_id: idInput.value.trim(),
        });
        await refresh();
      } catch (e) { showError(e.message); }
    });
    const rejectBtn = el("button", "Reject");
    rejectBtn.type = "button";
    rejectBtn.addEventListener("click", async () => {
      try {
        clearError();
        await apiSend("POST", "/admin/enroll/reject", { install_uuid: r.install_uuid });
        await refresh();
      } catch (e) { showError(e.message); }
    });
    actionCell.appendChild(approveBtn);
    actionCell.appendChild(rejectBtn);
    tr.appendChild(actionCell);

    body.appendChild(tr);
  }
}

// --- instances ---------------------------------------------------------------
async function renderInstances() {
  const body = document.getElementById("instances-body");
  const empty = document.getElementById("instances-empty");
  const data = await apiGet("/admin/instances");
  body.textContent = "";
  const rows = data.instances || [];
  empty.hidden = rows.length > 0;
  for (const inst of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", inst.id));
    tr.appendChild(el("td", inst.title));            // operator/suggested title -> textContent
    tr.appendChild(el("td", inst.status));
    tr.appendChild(el("td", inst.connected ? "yes" : "no"));

    const actionCell = el("td");
    const revokeBtn = el("button", "Revoke");
    revokeBtn.type = "button";
    revokeBtn.addEventListener("click", async () => {
      try {
        clearError();
        await apiSend(
          "POST",
          "/admin/instances/" + encodeURIComponent(inst.id) + "/revoke",
          {}
        );
        await refresh();
      } catch (e) { showError(e.message); }
    });
    actionCell.appendChild(revokeBtn);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}

async function refresh() {
  await Promise.all([renderWindow(), renderRequests(), renderInstances()]);
}

// --- wiring ------------------------------------------------------------------
function wireButtons() {
  document.getElementById("open-window").addEventListener("click", async () => {
    try {
      clearError();
      await apiSend("POST", "/admin/enroll/window", {});
      await renderWindow();
    } catch (e) { showError(e.message); }
  });
  document.getElementById("close-window").addEventListener("click", async () => {
    try {
      clearError();
      await apiSend("DELETE", "/admin/enroll/window", undefined);
      await renderWindow();
    } catch (e) { showError(e.message); }
  });
  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await apiSend("POST", "/admin/logout", {});
    } catch (_e) { /* logout is best-effort */ }
    window.location = "/admin/login";
  });
}

window.addEventListener("DOMContentLoaded", () => {
  wireButtons();
  refresh().catch((e) => showError(e.message));
});
