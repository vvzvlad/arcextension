// Curator admin console logic. Served from /admin/app.js under script-src 'self' (there is
// NO inline script). It renders the /admin JSON API through same-origin fetch.
//
// There is no pending-requests section anymore. Enrolment is one step (§6): a browser that
// submits the window code enrols itself under the id typed in its own settings, so there is
// nothing here to approve and no list of waiting rows. A REFUSED attempt is therefore not
// visible here either — it is reported in that browser's settings and counted in /metrics
// (`curator-enroll-id-taken` alerts on the collision case). That is the accepted cost of
// dropping the second step.
//
// SECURITY (issue #36 acc 6): every value rendered here is written with element.textContent
// — this file assigns raw markup to no element — so a value like `<img src=x onerror=…>`
// renders as literal text, never as markup. That still matters for the id column: an id is
// bounded to [A-Za-z0-9._-] server-side, but the rule is enforced there, not here.
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
// Builds the Error a failed response is reported with — used by BOTH wrappers below, so a
// read (apiGet) shows the operator the same words a write (apiSend) does. Both render*
// calls go through apiGet, so leaving it on a bare status meant a degraded
// service printed "/admin/instances -> 503" and threw away the sentence the server had
// already written.
//
// The Error carries `.status`: a caller that must react to a SPECIFIC status (the 409 the
// MAIN-revoke guard answers with) cannot parse it back out of the message.
//
// The error TEXT is read from both shapes, and that is not a nicety. `_http_exception`
// (src/app.py) renders a dict `detail` as JSON and every OTHER `detail` — i.e. nearly all
// of them — as plain text. Reading only `res.json().error` meant every carefully worded
// string detail was swallowed by the failed parse and the operator was shown a bare
// status code. The MAIN-revoke 409 spells out what revoking MAIN does and does not do; a
// lone "-> 409" tells the operator none of that.
const ERROR_DETAIL_MAX = 300; // one-line error box: enough for a sentence, not a page

async function responseError(method, path, res) {
  // The body is read ONCE, as text, and parsed from that string. Calling res.json() first
  // and res.text() as a fallback cannot work: json() consumes the body even when the parse
  // fails, so the fallback would only ever throw "body already read" and the plain-text
  // detail would stay invisible — the exact bug this replaces.
  let raw = "";
  try {
    raw = await res.text();
  } catch (_e) { /* body unreadable (network cut mid-response): keep the status code */ }
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch (_e) { /* plain-text detail: the raw body IS the message */ }
  // Only a STRING `error` field is a message. Testing `parsed.error` alone was wrong in
  // both directions: valid JSON WITHOUT that key (or a bare scalar like `409`) took the
  // JSON branch and reported `undefined`, while pairing the text branch with `!parsed`
  // dropped the body for exactly those responses. Anything that is not a string `error`
  // means the words are in the raw body, so fall back to it — and to the status code only
  // when there are no words at all.
  const message =
    parsed && typeof parsed.error === "string" && parsed.error.trim() ? parsed.error : raw;
  // Collapse the whitespace (the server wraps long details across source lines) and cap
  // it — this lands in a single-line error box.
  const text = message.trim().replace(/\s+/g, " ");
  let detail = res.status;
  if (text) {
    detail = text.length > ERROR_DETAIL_MAX ? text.slice(0, ERROR_DETAIL_MAX) + "…" : text;
  }
  const err = new Error(method + " " + path + " -> " + detail);
  err.status = res.status;
  return err;
}

async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (res.status === 401) {
    window.location = "/admin/login";
    throw new Error("unauthenticated");
  }
  if (!res.ok) throw await responseError("GET", path, res);
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
  if (!res.ok) throw await responseError(method, path, res);
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
    tr.appendChild(el("td", inst.id));               // the id IS the name (§6)
    tr.appendChild(el("td", inst.status));
    tr.appendChild(el("td", inst.connected ? "yes" : "no"));

    const actionCell = el("td");
    const revokeBtn = el("button", "Revoke");
    revokeBtn.type = "button";
    // Revoking the CONFIGURED MAIN is refused unless the request repeats that id in
    // `replacement` (src/db/queries.py `revoke_instance`) — a deliberate "say it twice"
    // guard, not a way to hand MAIN to another instance. This console cannot know WHICH
    // row is MAIN: /admin/instances does not say, and MAIN_INSTANCE_ID is service ENV. So
    // the first click always goes without `replacement`; a 409 is what identifies the row
    // as MAIN, and only then does the button arm and say what revoking MAIN does — and,
    // just as important, what it does NOT do. Without this the button was simply broken
    // for MAIN: it could only ever produce a bare "409".
    let mainConfirmArmed = false;
    revokeBtn.addEventListener("click", async () => {
      try {
        clearError();
        await apiSend(
          "POST",
          "/admin/instances/" + encodeURIComponent(inst.id) + "/revoke",
          mainConfirmArmed ? { replacement: inst.id } : {}
        );
        await refresh();
      } catch (e) {
        if (e.status === 409 && !mainConfirmArmed) {
          mainConfirmArmed = true;
          revokeBtn.textContent = "Confirm revoke of MAIN";
          showError(
            "«" + inst.id + "» is the MAIN instance (MAIN_INSTANCE_ID). Revoking it wipes " +
            "its credential and it must enrol again; meanwhile the curator still routes " +
            "drained tabs to this id, because MAIN is service configuration. To make a " +
            "DIFFERENT instance MAIN, change MAIN_INSTANCE_ID in .env and restart the " +
            "service — this button cannot do that. Press «Confirm revoke of MAIN» to " +
            "revoke it anyway."
          );
          return;
        }
        showError(e.message);
      }
    });
    actionCell.appendChild(revokeBtn);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}

async function refresh() {
  await Promise.all([renderWindow(), renderInstances()]);
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
