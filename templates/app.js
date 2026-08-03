// Curator admin console logic. Served from /admin/app.js under script-src 'self' (there is
// NO inline script). It renders the #35 /admin JSON API through same-origin fetch.
//
// SECURITY (issue #36 acc 6): suggested_title and origin are UNAUTHENTICATED input. They are
// ONLY ever written with element.textContent — this file assigns raw markup to no element —
// so a value like `<img src=x onerror=alert(1)>` renders as literal text, never as markup.
"use strict";

// How many chars of install_uuid this page prints. KEEP IN SYNC with the extension's
// INSTALL_UUID_PREFIX_LEN (extension/src/constants.js, mirrored in pages/options.js):
// the operator's job is to compare the string shown on an extension's options page with
// a row here, and two different prefix lengths cannot be compared at a glance.
const INSTALL_UUID_PREFIX_LEN = 18;

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
// read (apiGet) shows the operator the same words a write (apiSend) does. The three
// render* calls all go through apiGet, so leaving it on a bare status meant a degraded
// service printed "/admin/enroll/requests -> 503" and threw away the sentence the server
// had already written.
//
// The Error carries `.status`: a caller that must react to a SPECIFIC status (the 409 the
// MAIN-revoke guard answers with) cannot parse it back out of the message.
//
// The error TEXT is read from both shapes, and that is not a nicety. `_http_exception`
// (src/app.py) renders a dict `detail` as JSON and every OTHER `detail` — i.e. nearly all
// of them — as plain text. Reading only `res.json().error` meant every carefully worded
// string detail was swallowed by the failed parse and the operator was shown a bare
// status code. /admin/enroll/approve alone answers 409 with THREE different meanings
// (window closed / instance id already active / secret already enrolled), and the closed
// one even spells out the fix ("open it (POST /admin/enroll/window) and approve within
// it"); a lone "-> 409" tells the operator none of that.
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
    // NOT `install_uuid_short` (the server's first-8). The operator's only way to tell
    // their own request from someone else's is to compare this string with the one the
    // extension's options page shows — and the other two columns do not help: the origin
    // is uniform fleet-wide and two browsers of the same person carry the same suggested
    // title. 8 hex chars collide too easily for a decision that grants a credential, so
    // both places print the same INSTALL_UUID_PREFIX_LEN chars of the same value, and the
    // full uuid is on the cell as a tooltip. (A property assignment, not markup — the
    // textContent-only contract for untrusted fields is untouched.)
    const uuidCell = el("td", (r.install_uuid || "").slice(0, INSTALL_UUID_PREFIX_LEN));
    uuidCell.title = r.install_uuid || "";
    tr.appendChild(uuidCell);
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
