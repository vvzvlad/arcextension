// Curator admin console logic. Served from /admin/app.js under script-src 'self' (there is
// NO inline script). It renders the /admin JSON API through same-origin fetch.
//
// There is no pending-requests section anymore. Enrolment is one step (§6): a browser that
// submits the registration code enrols itself under the id typed in its own settings, so
// there is nothing here to approve and no list of waiting rows. A REFUSED attempt is
// therefore not visible here either — it is reported in that browser's settings and counted
// in /metrics (`curator-enroll-id-taken` alerts on the collision case). That is the accepted
// cost of dropping the second step.
//
// SECURITY (issue #36 acc 6): every value rendered here is written with element.textContent
// — this file assigns raw markup to no element — so a value like `<img src=x onerror=…>`
// renders as literal text, never as markup. That still matters for the id column: an id is
// bounded to [A-Za-z0-9._-] server-side, but the rule is enforced there, not here.
//
// The page CSP also forbids the `style` attribute (`style-src 'self'`, no 'unsafe-inline'),
// so NOTHING here writes a style: visual state is a class toggle, and the countdown bar is a
// <progress> whose `value`/`max` are content attributes.
//
// UI language is Russian, and the word "окно" is not part of it: `enroll window` is the
// API's noun. The operator opens and closes РЕГИСТРАЦИЮ; the element ids still say
// "window" because they are the API's names and templates/admin.html is keyed on them.
"use strict";

// --- small DOM helpers (textContent only) ------------------------------------
function el(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) {
    node.textContent = String(text); // text node only: untrusted values stay inert markup
  }
  return node;
}

function byId(id) {
  return document.getElementById(id);
}

// Russian plural for a count: 1 браузер / 2 браузера / 5 браузеров.
function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

// --- error slots -------------------------------------------------------------
// A refusal is shown NEXT TO the action that caused it (`setSlot` on the block's own
// <p class="inline-error">). The page-wide #error box carries only what belongs to no
// button — the initial load failing — so a message never has to be hunted for at the top of
// the page after pressing something at the bottom of it.
function showError(message) {
  const box = byId("error");
  box.textContent = String(message);
  box.hidden = false;
}

function clearError() {
  const box = byId("error");
  box.textContent = "";
  box.hidden = true;
}

function setSlot(node, message) {
  if (node === null) return;
  node.textContent = String(message);
  node.hidden = false;
}

function clearSlot(node) {
  if (node === null) return;
  node.textContent = "";
  node.hidden = true;
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

// --- time formatting ---------------------------------------------------------
// Written out rather than taken from toLocaleDateString: the console must read the same on
// an operator's en-US browser as on a ru one, and the server hands out plain epoch ms.
const MONTHS = [
  "янв", "фев", "мар", "апр", "мая", "июн",
  "июл", "авг", "сен", "окт", "ноя", "дек",
];

const NO_VALUE = "—";

function absoluteDate(ts) {
  if (ts === null || ts === undefined) return NO_VALUE;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return NO_VALUE;
  return d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear();
}

// Whole minutes/hours/days since a SERVER timestamp, measured against this machine's clock.
// The two clocks can drift; at minute granularity a few seconds of skew is invisible, and a
// clock that is behind the server would otherwise produce a negative age — clamped to 0
// ("только что") rather than rendered as a time in the future.
function ageParts(ts, now) {
  const seconds = Math.max(0, Math.round((now - ts) / 1000));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  return { seconds: seconds, minutes: minutes, hours: hours, days: days };
}

function relativeTime(ts, now) {
  if (ts === null || ts === undefined) return NO_VALUE;
  const age = ageParts(ts, now);
  if (age.minutes < 1) return "только что";
  if (age.minutes < 60) return age.minutes + " мин назад";
  if (age.hours < 24) return age.hours + " ч назад";
  return age.days + " " + plural(age.days, "день", "дня", "дней") + " назад";
}

// The state word for an enrolled browser that is not on the wire right now.
function silenceLabel(ts, now) {
  if (ts === null || ts === undefined) return "ни разу не выходил на связь";
  const age = ageParts(ts, now);
  if (age.minutes < 1) return "молчит меньше минуты";
  if (age.minutes < 60) return "молчит " + age.minutes + " мин";
  if (age.hours < 24) return "молчит " + age.hours + " ч";
  return "молчит " + age.days + " " + plural(age.days, "день", "дня", "дней");
}

// "7:42" — the shape a countdown is read in, not a duration ("462 с").
function countdownText(seconds) {
  const total = Math.max(0, seconds);
  const s = total % 60;
  const m = Math.floor(total / 60);
  return m + ":" + String(s).padStart(2, "0");
}

// --- registration (the enrollment window) ------------------------------------
// The countdown ticks on the CLIENT, from the `seconds_remaining` the server reported: the
// server needs no timer (it compares the stored deadline against `now` on every read), and
// polling once a second just to redraw a number would be a request per second per open
// console.
//
// `total` is the denominator of the drain bar. On a window this console OPENED it is the
// full length, so the bar is the true fraction. On a console loaded MID-window the full
// length is not knowable from the read (`GET /admin/enroll/window` reports what is left,
// not what it started with), so the bar drains from full over whatever remains — it still
// empties at exactly the moment registration closes, which is what the bar is read for.
const enroll = {
  timer: null,     // setInterval handle — always cleared before a new one is started
  deadline: 0,     // client-clock ms when registration closes
  total: 0,        // seconds the bar drains over
  code: null,      // the code the running countdown belongs to
  copiedTimer: null, // the "скопировано" hint's reset timer
};

function stopCountdown() {
  if (enroll.timer !== null) {
    clearInterval(enroll.timer);
    enroll.timer = null;
  }
}

// Exactly one of the three states is on screen at a time.
function showWindowState(state) {
  byId("window-status").hidden = state !== "unknown";
  byId("window-closed").hidden = state !== "closed";
  byId("window-open").hidden = state !== "open";
  byId("window-section").classList.toggle("is-open", state === "open");
}

// The code is grouped in threes so it can be read aloud and typed on another machine. The
// groups are separate elements with a MARGIN between them, never a space character: a space
// would travel with a copied selection, and the extension only trims the ends of what is
// pasted into its field — an inner space would be sent to the server and refused.
function renderCode(code) {
  const box = byId("window-code");
  box.textContent = "";
  box.classList.remove("code-empty");
  if (!code) {
    box.classList.add("code-empty");
    box.appendChild(el("span", NO_VALUE));
    return;
  }
  for (let i = 0; i < code.length; i += 3) {
    box.appendChild(el("span", code.slice(i, i + 3)));
  }
}

function tickCountdown() {
  const left = Math.max(0, Math.ceil((enroll.deadline - Date.now()) / 1000));
  byId("window-countdown").textContent = countdownText(left);
  const bar = byId("window-bar");
  bar.max = enroll.total > 0 ? enroll.total : 1;
  bar.value = Math.min(left, bar.max);
  if (left > 0) return;
  // Reached zero: flip the card ourselves rather than waiting for a read to agree. The
  // deadline is the server's own (it answers `open` by comparing it to now), so the window
  // IS closed at this instant — leaving a live-looking code on screen for another round
  // trip is the one thing this card must not do. The re-read follows, and wins if the
  // clocks disagree.
  stopCountdown();
  enroll.code = null;
  showWindowState("closed");
  renderWindow().catch(() => { /* reported in place by renderWindow */ });
}

function startCountdown(seconds, code) {
  // A re-read of the SAME window keeps its denominator; a new code is a new window.
  if (code !== enroll.code) {
    enroll.code = code;
    enroll.total = seconds;
  }
  enroll.deadline = Date.now() + seconds * 1000;
  stopCountdown();
  tickCountdown();
  // tickCountdown() may have closed the card already (a zero-second read); do not arm a
  // timer for a window that is over.
  if (enroll.code !== null) {
    enroll.timer = window.setInterval(tickCountdown, 1000);
  }
}

async function renderWindow() {
  const status = byId("window-status");
  let w;
  try {
    w = await apiGet("/admin/enroll/window");
  } catch (e) {
    stopCountdown();
    enroll.code = null;
    status.textContent = "Состояние регистрации не прочитать: " + e.message;
    showWindowState("unknown");
    throw e;
  }
  if (w.open) {
    renderCode(w.code);
    showWindowState("open");
    startCountdown(Number(w.seconds_remaining) || 0, w.code || null);
    return;
  }
  stopCountdown();
  enroll.code = null;
  // The button names the length the SERVER is configured for (ENROLL_WINDOW_MIN), so the
  // promise on it is the one the service keeps.
  const minutes = Number(w.window_minutes);
  byId("open-window").textContent =
    minutes > 0
      ? "Открыть регистрацию на " + minutes + " " + plural(minutes, "минуту", "минуты", "минут")
      : "Открыть регистрацию";
  showWindowState("closed");
}

// The default text under the code. Restored after the transient copy feedback, so it has to
// match the copy in templates/admin.html.
const COPY_HINT = "Введите этот код в настройках браузера, который регистрируете.";

async function copyCode() {
  const hint = byId("copy-hint");
  const code = Array.from(byId("window-code").childNodes)
    .map((n) => n.textContent)
    .join("");
  if (enroll.copiedTimer !== null) {
    clearTimeout(enroll.copiedTimer);
    enroll.copiedTimer = null;
  }
  const restore = () => {
    enroll.copiedTimer = null;
    hint.textContent = COPY_HINT;
  };
  try {
    await navigator.clipboard.writeText(code);
    hint.textContent = "Код скопирован.";
  } catch (_e) {
    // Clipboard access needs a secure context; over plain http it simply is not there.
    hint.textContent = "Скопировать не удалось — выделите код и скопируйте вручную.";
  }
  enroll.copiedTimer = window.setTimeout(restore, 4000);
}

// --- instances ---------------------------------------------------------------
// The MAIN id as the SERVER reports it (`GET /admin/instances`). The console cannot derive
// it — MAIN_INSTANCE_ID is service configuration — and without it the MAIN row cannot be
// marked, nor its revoke explained before it is sent instead of after a 409.
let mainInstanceId = null;

const EMPTY_TEXT = "Ни один браузер ещё не зарегистрирован.";

// A 409 from a revoke means exactly one thing (src/db/queries.py `revoke_instance`): the
// target is the configured MAIN and the request did not repeat its id. Both call sites send
// the id the list reported, so reaching this means MAIN changed under us. The server's
// sentence is kept — it names the guard — but it is written in the API's language and about
// the API's field, so it is introduced in the page's.
const MAIN_CHANGED_NOTE =
  "Главный браузер сменился с момента, когда был прочитан список — список обновлён. " +
  "Ответ сервера: ";

function revokeRefusalText(err) {
  return err.status === 409 ? MAIN_CHANGED_NOTE + err.message : err.message;
}

// green = on the wire, amber = enrolled but silent, grey = revoked. Same dictionary as the
// startpage status bar, so one glance means the same thing on both pages.
function instanceState(inst, now) {
  if (inst.status === "revoked") return { dot: "grey", text: "отозван" };
  if (inst.status !== "active") return { dot: "grey", text: String(inst.status) };
  if (inst.connected) return { dot: "ok", text: "активен" };
  return { dot: "warn", text: silenceLabel(inst.last_seen_at, now) };
}

// Reading order: MAIN first (it is the one row whose loss changes how the curator routes),
// then the browsers that are on the wire, then the silent ones, then the revoked. The server
// returns the rows by id — an order in which a revoked browser retired last month can sit
// between two live ones.
function fleetRank(inst) {
  if (mainInstanceId !== null && inst.id === mainInstanceId) return 0;
  if (inst.status === "revoked") return 3;
  return inst.connected ? 1 : 2;
}

function sortedFleet(rows) {
  return rows.slice().sort((a, b) => {
    const byRank = fleetRank(a) - fleetRank(b);
    if (byRank !== 0) return byRank;
    return String(a.id).localeCompare(String(b.id), "ru");
  });
}

function fleetCount(rows) {
  const revoked = rows.filter((r) => r.status === "revoked").length;
  const total = rows.length;
  let text = total + " " + plural(total, "браузер", "браузера", "браузеров");
  if (revoked > 0) {
    text += " · " + revoked + " " + plural(revoked, "отозван", "отозвано", "отозвано");
  }
  return text;
}

// The in-place confirm/refusal for ONE row: a full-width strip under the row it belongs to.
// Only one is ever open — a second click elsewhere replaces it.
function closeRowPanel() {
  const open = document.querySelector("tr.row-panel");
  if (open !== null) open.remove();
}

function openRowPanel(afterRow, text) {
  closeRowPanel();
  const tr = el("tr");
  tr.className = "row-panel";
  const td = el("td");
  td.colSpan = 5;
  const box = el("div");
  box.className = "row-panel-box";
  const p = el("p", text);
  p.className = "row-panel-text";
  box.appendChild(p);
  const errorSlot = el("p");
  errorSlot.className = "inline-error";
  errorSlot.hidden = true;
  td.appendChild(box);
  tr.appendChild(td);
  afterRow.after(tr);
  return { box: box, errorSlot: errorSlot };
}

// Revoke an ordinary (non-MAIN) instance: confirmed in place, one strip under its row.
function confirmOrdinaryRevoke(tr, instanceId) {
  const panel = openRowPanel(
    tr,
    "Отозвать «" + instanceId + "»? Браузер потеряет доступ и вернётся только новой " +
      "регистрацией под тем же именем."
  );
  const confirm = el("button", "Отозвать");
  confirm.type = "button";
  confirm.className = "btn btn-mini btn-danger";
  const cancel = el("button", "Отмена");
  cancel.type = "button";
  cancel.className = "btn btn-mini";
  panel.box.appendChild(confirm);
  panel.box.appendChild(cancel);
  panel.box.appendChild(panel.errorSlot);
  cancel.addEventListener("click", closeRowPanel);
  confirm.addEventListener("click", async () => {
    clearSlot(panel.errorSlot);
    confirm.disabled = true;
    try {
      await apiSend("POST", "/admin/instances/" + encodeURIComponent(instanceId) + "/revoke", {});
    } catch (e) {
      confirm.disabled = false;
      // 409 = this row turned out to be MAIN after all (the list is a snapshot; MAIN is
      // service configuration and can have changed under us). Say so where the click was,
      // and let the refresh below re-mark the row.
      setSlot(panel.errorSlot, revokeRefusalText(e));
      if (e.status === 409) renderInstances().catch(() => {});
      return;
    }
    await renderInstances().catch(() => {});
  });
}

// Revoke MAIN: the dedicated block, opened BEFORE the request goes out. What it says is
// what this button can actually do — it wipes MAIN's credential, and it does NOT hand the
// MAIN role to anyone else, because the role is MAIN_INSTANCE_ID in the service's own
// configuration and nothing served over HTTP can move it.
function openMainRevoke(instanceId) {
  closeRowPanel();
  const section = byId("revoke-main-section");
  byId("revoke-main-id").textContent = instanceId;
  clearSlot(byId("revoke-main-error"));
  byId("revoke-main-confirm").disabled = false;
  section.hidden = false;
  section.scrollIntoView({ block: "nearest" });
}

function closeMainRevoke() {
  byId("revoke-main-section").hidden = true;
  clearSlot(byId("revoke-main-error"));
}

async function submitMainRevoke() {
  const instanceId = byId("revoke-main-id").textContent;
  const confirm = byId("revoke-main-confirm");
  const errorSlot = byId("revoke-main-error");
  if (!instanceId) return;
  clearSlot(errorSlot);
  confirm.disabled = true;
  try {
    // `replacement` repeats the CURRENT MAIN id: the server refuses a MAIN revoke that does
    // not (src/db/queries.py `revoke_instance`) — a deliberate "say it twice" guard. We know
    // the id because the list reports it, so the request is right the first time and the
    // operator never meets a dead end.
    await apiSend(
      "POST", "/admin/instances/" + encodeURIComponent(instanceId) + "/revoke",
      { replacement: mainInstanceId }
    );
  } catch (e) {
    confirm.disabled = false;
    // The 409 handling stays as the safety net for the one case the pre-filled request
    // cannot cover: MAIN changed between the read and this click (a restart with a
    // different MAIN_INSTANCE_ID). The server's own sentence explains it.
    setSlot(errorSlot, revokeRefusalText(e));
    if (e.status === 409) renderInstances().catch(() => {});
    return;
  }
  closeMainRevoke();
  await renderInstances().catch(() => {});
}

function instanceRow(inst, now) {
  const tr = el("tr");
  const isRevoked = inst.status === "revoked";
  if (isRevoked) tr.className = "is-revoked";
  const state = instanceState(inst, now);

  const nameCell = el("td");
  const dot = el("span");
  dot.className = "dot " + state.dot;
  nameCell.appendChild(dot);
  const name = el("span", inst.id);                  // the id IS the name (§6)
  name.className = "name";
  nameCell.appendChild(name);
  if (mainInstanceId !== null && inst.id === mainInstanceId) {
    const tag = el("span", "MAIN");
    tag.className = "tag";
    nameCell.appendChild(tag);
  }
  tr.appendChild(nameCell);

  tr.appendChild(el("td", state.text));

  const seen = el("td", isRevoked ? NO_VALUE : relativeTime(inst.last_seen_at, now));
  seen.className = "num";
  tr.appendChild(seen);

  const enrolled = el("td", absoluteDate(inst.enrolled_at));
  enrolled.className = "num";
  tr.appendChild(enrolled);

  const actionCell = el("td");
  actionCell.className = "act";
  if (isRevoked) {
    const gone = el(
      "span",
      inst.revoked_at ? "отозван " + absoluteDate(inst.revoked_at) : "отозван"
    );
    gone.className = "gone";
    actionCell.appendChild(gone);
  } else {
    const revokeBtn = el("button", "Отозвать");
    revokeBtn.type = "button";
    revokeBtn.className = "btn btn-mini is-revoke";
    revokeBtn.addEventListener("click", () => {
      if (mainInstanceId !== null && inst.id === mainInstanceId) {
        openMainRevoke(inst.id);
        return;
      }
      confirmOrdinaryRevoke(tr, inst.id);
    });
    actionCell.appendChild(revokeBtn);
  }
  tr.appendChild(actionCell);
  return tr;
}

async function renderInstances() {
  const body = byId("instances-body");
  const empty = byId("instances-empty");
  const count = byId("instances-count");
  let data;
  try {
    data = await apiGet("/admin/instances");
  } catch (e) {
    body.textContent = "";
    count.textContent = "";
    empty.textContent = "Список браузеров не прочитать: " + e.message;
    empty.hidden = false;
    throw e;
  }
  const rows = data.instances || [];
  mainInstanceId = typeof data.main_instance_id === "string" ? data.main_instance_id : null;
  // A MAIN-revoke block left open for a row that is gone (or is no longer MAIN) would
  // arm a button against a stale target.
  const pending = byId("revoke-main-id").textContent;
  if (pending && pending !== mainInstanceId) closeMainRevoke();
  closeRowPanel();
  body.textContent = "";
  empty.textContent = EMPTY_TEXT;
  empty.hidden = rows.length > 0;
  count.textContent = rows.length > 0 ? fleetCount(rows) : "";
  const now = Date.now();
  for (const inst of sortedFleet(rows)) {
    body.appendChild(instanceRow(inst, now));
  }
}

async function refresh() {
  await Promise.all([renderWindow(), renderInstances()]);
}

// --- wiring ------------------------------------------------------------------
function wireButtons() {
  const windowError = byId("window-error");

  byId("open-window").addEventListener("click", async () => {
    clearSlot(windowError);
    try {
      await apiSend("POST", "/admin/enroll/window", {});
    } catch (e) {
      setSlot(windowError, e.message);
      return;
    }
    await renderWindow().catch(() => { /* reported in place by renderWindow */ });
  });

  byId("close-window").addEventListener("click", async () => {
    clearSlot(windowError);
    try {
      await apiSend("DELETE", "/admin/enroll/window", undefined);
    } catch (e) {
      setSlot(windowError, e.message);
      return;
    }
    await renderWindow().catch(() => { /* reported in place by renderWindow */ });
  });

  byId("copy-code").addEventListener("click", () => {
    copyCode();
  });

  byId("revoke-main-confirm").addEventListener("click", () => {
    submitMainRevoke();
  });
  byId("revoke-main-cancel").addEventListener("click", closeMainRevoke);

  byId("logout").addEventListener("click", async () => {
    stopCountdown();
    try {
      await apiSend("POST", "/admin/logout", {});
    } catch (_e) { /* logout is best-effort */ }
    window.location = "/admin/login";
  });
}

window.addEventListener("DOMContentLoaded", () => {
  wireButtons();
  clearError();
  refresh().catch((e) => showError("Не удалось загрузить состояние консоли. " + e.message));
});
