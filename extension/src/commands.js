// Command dispatcher (§6 "Команды (сервис → расширение)").
//
// A `command {id, sessionId, command, params}` frame arriving on the /ext socket
// is executed here and answered with `response {id, ok, result|error}`
// (`error = {code, message}`). This module owns ONLY the outcome part
// (`{ok, result}` / `{ok, error}`); connection.js stamps `type` + `id` and sends
// it back over the socket.
//
// Two invariants that are the whole point of this phase (§6, §12):
//
//   1. The volatile close_tab guards are RE-CHECKED AT THE EDGE, live, against
//      chrome.tabs.get / chrome.windows.getLastFocused / the own activity map —
//      never against the snapshot, which is already minutes old by command time
//      (a 200-tab pass runs for minutes; in that window a background tab can
//      start playing audio, get pinned, be re-viewed, or fall out of idle).
//   2. open_tab / navigate_tab accept ONLY http/https, validated HERE at the
//      edge — otherwise a caller could steer a tab to `data:`/`javascript:` via a
//      rule's canonical_url, bypassing the entire gate built around execute_js.
//
// The module uses the global `chrome` (like activity-map.js / snapshot.js) and
// imports the activity map directly; both are overridable through `ctx` so the
// dispatcher is unit-testable without a browser.

import * as activityMap from "./activity-map.js";
import {
  ALLOW_EXECUTE_JS_KEY,
  CMD_OPEN_TAB,
  CMD_CLOSE_TAB,
  CMD_GET_TAB,
  CMD_FOCUS_TAB,
  CMD_FOCUS_WINDOW,
  CMD_NAVIGATE_TAB,
  CMD_MERGE_WINDOWS,
  CMD_EXECUTE_JS,
  CMD_MOVE_TAB,
  CMD_GET_TEXT,
  CMD_WAIT_FOR,
  CMD_SCROLL_UNTIL,
  CMD_START_JS,
  CMD_POLL_JOB,
  CMD_SET_FOCUS_EMULATION,
  ERR_STALE_SESSION,
  ERR_PRECONDITION_FAILED,
  ERR_NO_SUCH_TAB,
  ERR_NO_WINDOW,
  ERR_JS_DISABLED,
  ERR_BUSY_DRAGGING,
  ERR_PINNED_CROSS_WINDOW,
  ERR_DEBUGGER_ATTACH,
  ERR_INTERNAL,
  WAIT_POLL_MS,
  WAIT_COMMIT_GRACE_POLLS,
  WAIT_MAX_TIMEOUT_MS,
} from "./constants.js";

// --- small helpers ----------------------------------------------------------

function ok(result) {
  return { ok: true, result: result || {} };
}

function fail(code, message) {
  return { ok: false, error: { code, message: message || code } };
}

// http/https ONLY (§12). A non-string, an unparseable value, or any other scheme
// (`data:`, `javascript:`, `chrome:`, `file:`, `blob:` …) is rejected at the
// edge so the execute_js gate cannot be smuggled past via a tab URL.
export function isHttpUrl(url) {
  if (typeof url !== "string") return false;
  let u;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  return u.protocol === "http:" || u.protocol === "https:";
}

// --- injected function bodies ------------------------------------------------
//
// EVERY function below is injected into a page by chrome.scripting, which serializes it
// TO SOURCE. So each MUST be top-level and closure-free: it cannot capture a module
// import, a module constant, or anything else from this file. Inner helpers declared
// INSIDE the function are fine — they travel with the source.
//
// They are exported ONLY so the tests can call them directly against a DOM double: the
// chrome mock's `scripting.executeScript` returns a canned value without ever running the
// function, so an un-exported injected body is untestable.
//
// THE SPLIT THAT MATTERS (§12): `evalInWorld` carries ARBITRARY code and is therefore
// behind the execute_js checkbox + a js_audit row. `readTextInWorld` and
// `matchInWorld` are FIXED — committed here, known at build time, taking only a selector
// or a substring — so there is nothing to reconstruct after the fact and they are NOT
// behind that gate and write NO audit row. They are still subject to every other gate: the
// session check above, the service-side pause/stop and revoke checks, and the http/https
// edge guard on the target tab.

// The body injected into the target world by execute_js.
//
// `awaitPromise` (default false) buys the two KEYWORDS indirect eval cannot parse: a
// top-level `await`, and a top-level `return` (a SyntaxError in eval). It is NOT what
// makes async code work in general — chrome.scripting awaits a promise the injected
// function returns, so `fetch(u).then(r => r.json())` resolves on the default path and
// always has. Reach for the flag when the snippet wants to WRITE `await`/`return`.
//
// AsyncFunction, not `new Function("(async()=>{" + source + "})()")`: string-splicing the
// source into a wrapper breaks on a source whose last line is a `//` comment — the
// appended `})()` lands inside that comment and the whole thing is a SyntaxError. The
// constructor takes the body verbatim and supplies the braces itself.
//
// It is compiled in TWO STEPS, the strategy a REPL uses, because the constructor makes the
// source the function BODY — which throws an expression's completion value away. Compiled
// only that way, `awaitPromise:true` would answer null for `document.title`: exactly the
// silent null this flag exists to remove, and worse, since an agent that turns the flag on
// for every call would get nulls everywhere. So: EXPRESSION first, statements as fallback.
export function evalInWorld(source, awaitPromise) {
  // chrome.scripting structured-clones the result on its way out of the page, and a value
  // that cannot be cloned — a DOM node, a function, a circular object, a Window — becomes
  // a silent `null`. That reads exactly like "the code returned null", which is the single
  // most confusing failure this verb has. Name it instead.
  const describe = (v) => {
    // A PROMISE is CHAINED, never cloned — and this test must come BEFORE the probe.
    // chrome.scripting awaits a promise the injected function returns, so
    // `Promise.resolve(42)` and `fetch(u).then(r => r.json())` have always worked with no
    // flag at all; but structuredClone throws DataCloneError on a promise, so probing one
    // would report `{__unserializable:"Promise"}` and swallow the data the agent asked
    // for. Chaining puts the probe on the RESOLVED value — the one that actually crosses
    // the boundary — which is where it belonged all along.
    //
    // The `.then` read is guarded for the same reason the `.constructor` read below is: an
    // exotic proxy can throw on property access. A throw here just falls through to the
    // probe, which is already wrapped.
    let thenable = false;
    try {
      thenable =
        !!v && (typeof v === "object" || typeof v === "function") && typeof v.then === "function";
    } catch {
      thenable = false;
    }
    if (thenable) return v.then(describe);
    // MAIN world shares the page's globals, and a page can delete structuredClone. Without
    // this guard the probe would throw for EVERY value and report each one unserializable —
    // turning a diagnostic into a fabrication. No probe available => say nothing, which is
    // exactly today's behaviour.
    if (typeof structuredClone !== "function") return v;
    try {
      structuredClone(v);
      return v;
    } catch {
      let kind = typeof v;
      try {
        if (v && v.constructor && v.constructor.name) kind = v.constructor.name;
      } catch {
        // An exotic proxy can throw on `.constructor`; `typeof` is still an answer.
      }
      let preview = "";
      try {
        preview = String(v).slice(0, 200);
      } catch {
        preview = "<unstringifiable>";
      }
      return { __unserializable: kind, preview };
    }
  };
  if (!awaitPromise) {
    // Indirect eval — today's path, unchanged. The ONLY observable difference is that a
    // result which used to arrive as `null` because it could not be cloned now names
    // itself; a value that clones fine is returned byte for byte as before.
    // eslint-disable-next-line no-eval
    return describe((0, eval)(source));
  }
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  // Step 1 — compile the source as an EXPRESSION, so its completion value survives
  // (`document.title` answers the title, not null). The trailing NEWLINE before `)` is
  // load-bearing: a source ending in a `//` comment would otherwise swallow the closing
  // paren — the same hazard that rules out splicing an IIFE, one wrapper layer down.
  //
  // Step 2 — retry the expression with trailing whitespace and semicolons stripped.
  // `document.title;` is the ORDINARY way to write a one-liner, and that semicolon inside
  // the parens is a SyntaxError: without this step the most common shape of all fell
  // through to the statement body, which has no `return`, and the value was lost — the
  // silent null this flag exists to remove, back again. The trim is safe because it removes
  // ONLY trailing `;` and whitespace, and neither can change an EXPRESSION's value; code
  // that is genuinely more than one statement (`const a = 1; a;`) still fails to parse
  // inside the parens and still reaches step 3. The trailing NEWLINE is load-bearing here
  // for the same reason as in step 1 — the trim stops at the first non-`;`/non-space
  // character, so a source ending in a `//` comment is left exactly as it was.
  //
  // Step 3 — a SyntaxError from BOTH expression attempts means it was never an expression
  // (`const r = await f(); return r.status`), so compile the ORIGINAL, untouched source as
  // the BODY, where writing `return` is the agent's job. Only a SyntaxError falls back: any
  // other constructor failure is real and must surface. Compiling up to three times is free
  // of side effects — no step RUNS the code.
  let fn;
  try {
    fn = new AsyncFunction(`return (${source}\n);`);
  } catch (e) {
    if (!(e instanceof SyntaxError)) throw e;
    try {
      fn = new AsyncFunction(`return (${source.replace(/[\s;]+$/, "")}\n);`);
    } catch (e2) {
      if (!(e2 instanceof SyntaxError)) throw e2;
      fn = new AsyncFunction(source);
    }
  }
  // Returning the promise is what makes chrome.scripting await it.
  return fn().then(describe);
}

// The body injected by get_text. Returns `{found, text, totalBytes, truncated?}`.
//
// `found:false` (a selector that matched nothing) is NOT an empty string: an empty string
// reads as "the page is blank", which is a different fact and would send the agent
// debugging the page instead of its selector.
//
// The cut happens HERE, not only on the service, for two reasons: this side is the only
// one that knows the TRUE size (so `totalBytes` is honest rather than "the size of what
// we already truncated"), and a 10 MB innerText never has to cross the socket at all.
export function readTextInWorld(selector, maxBytes) {
  let el;
  if (selector) {
    try {
      el = document.querySelector(selector);
    } catch (e) {
      // A malformed selector (`#a:has(>`) is the CALLER's typo. Unreported it escapes the
      // injection as a rejected promise and reaches the agent as `internal` — a code that
      // says "our bug", sending them to read our logs instead of their selector. Carried
      // back as a VALUE because a throw is indistinguishable from a torn-down frame.
      if (e && e.name === "SyntaxError") {
        return { found: false, badSelector: true, message: String((e && e.message) || e) };
      }
      throw e;
    }
  } else {
    el = document.body;
  }
  if (!el) return { found: false, text: "", totalBytes: 0 };
  const text = el.innerText ?? "";
  const bytes = new TextEncoder().encode(text);
  const limit = typeof maxBytes === "number" && maxBytes > 0 ? maxBytes : 0;
  if (!limit || bytes.length <= limit) {
    return { found: true, text, totalBytes: bytes.length };
  }
  // Cut on a CHARACTER boundary. Slicing the byte array can land mid-sequence, and a
  // plain decode of that would end the text in U+FFFD; `{stream:true}` holds back the
  // incomplete trailing sequence instead (the decoder is discarded, so it is never
  // flushed). Costs at most 3 dropped bytes, never a mojibake tail.
  const cut = new TextDecoder("utf-8").decode(bytes.slice(0, limit), { stream: true });
  return { found: true, text: cut, totalBytes: bytes.length, truncated: true };
}

// The body injected by wait_for's `selector` / `textContains` predicates, and by
// navigate_tab's `waitUntil:'selector'`. Returns `{matched}` — or `{badSelector, message}`
// for a selector that does not parse, which the caller must NOT mistake for "not yet".
export function matchInWorld(selector, textContains) {
  if (selector) {
    try {
      return { matched: !!document.querySelector(selector) };
    } catch (e) {
      // Same reasoning as readTextInWorld, and here it costs more: an injection that
      // THROWS is read by pollUntil as a frame being torn down mid-navigation, so a typo'd
      // selector would poll to the deadline and then report "condition not met" — a whole
      // minute spent to answer the wrong question.
      if (e && e.name === "SyntaxError") {
        return { badSelector: true, message: String((e && e.message) || e) };
      }
      throw e;
    }
  }
  // innerText, not textContent, and the reflow it forces (up to ~240 layouts across a 60 s
  // wait) is the price: textContent also returns text inside `display:none` templates and
  // `<script>`/`<style>` bodies, so an SPA that ships its success banner hidden in the
  // markup would match on the FIRST poll. A wait that returns before the thing is on
  // screen is worse than a slow one — and it also keeps this predicate agreeing with
  // get_text, which is where the agent read the text it is now waiting for.
  const text = (document.body && document.body.innerText) || "";
  return { matched: text.includes(textContains) };
}

// The body injected by scroll_until on EVERY step. FIXED like readTextInWorld/matchInWorld
// (§12): `countSelector` / `containerSelector` are DATA handed to querySelector(All) and
// `direction` is one of two literals — nothing is spliced into an eval — so this verb needs
// no execute_js checkbox and writes no js_audit row.
//
// Scrolls the target (a container element, or the document viewport when
// `containerSelector` is null) to the far end for the direction, then answers
// `{count}` = how many `countSelector` matches the page now holds. A malformed selector is
// reported as a `{badSelector, message}` VALUE, never a throw — same reason as matchInWorld:
// the scroll_until loop reads a thrown injection as a frame being torn down and would poll
// to the deadline, turning a typo into a full budget spent on the wrong question. A
// `containerSelector` that matched nothing is `{noContainer}`, distinct from a bad selector.
export function scrollAndCountInWorld(containerSelector, direction, countSelector) {
  const up = direction === "up";
  if (containerSelector) {
    let scroller;
    try {
      scroller = document.querySelector(containerSelector);
    } catch (e) {
      if (e && e.name === "SyntaxError") {
        return { badSelector: true, message: String((e && e.message) || e) };
      }
      throw e;
    }
    if (!scroller) return { noContainer: true };
    // up => history/chat feeds load OLDER items by pulling to the top; down => the ordinary
    // infinite feed grows off the bottom.
    scroller.scrollTop = up ? 0 : scroller.scrollHeight;
  } else {
    // The whole document. `scrollingElement` (documentElement fallback) owns `scrollTop`;
    // `window.scrollTo` is the same move on the window, and doing both covers pages where
    // one path is a quirks-mode no-op.
    const doc = document.scrollingElement || document.documentElement;
    const target = up ? 0 : doc ? doc.scrollHeight : 0;
    if (doc) doc.scrollTop = target;
    if (typeof window !== "undefined" && typeof window.scrollTo === "function") {
      window.scrollTo(0, target);
    }
  }
  let count;
  try {
    count = document.querySelectorAll(countSelector).length;
  } catch (e) {
    if (e && e.name === "SyntaxError") {
      return { badSelector: true, message: String((e && e.message) || e) };
    }
    throw e;
  }
  return { count };
}

// The body injected by start_js. THIS is the arbitrary-code path (§12): `source` is the
// caller's code, which is precisely why start_js — like execute_js — is behind the
// execute_js checkbox + a js_audit row on the service. The body itself is
// fixed and committed; what it COMPILES is not.
//
// It wraps `source` in an async IIFE and does NOT await it: the result of executeScript is
// `{jobId}`, returned at once, while the promise runs on in the page and settles the job
// record under `window.__curatorJobs[jobId]` to `{state:'done', value}` or
// `{state:'error', message}`. `poll_job` (readJobInWorld) reads that record later.
//
// `source` is compiled the SAME three ways as evalInWorld's await_promise path — expression
// first (so a bare `fetch(u)` answers its value), then the trimmed expression (so a trailing
// `;` does not lose it), then the untouched body (where writing `return` is the caller's
// job). The describe() probe names a value structuredClone cannot carry out of the page,
// instead of storing a silent null. Both helpers are inlined because an injected function is
// serialized to source and cannot capture evalInWorld from this module.
export function startJobInWorld(jobId, source) {
  const describe = (v) => {
    // v is ALREADY the awaited value (the IIFE below awaits fn()), so — unlike evalInWorld —
    // there is no promise to chain here, only the clone probe. MAIN world can delete
    // structuredClone; with no probe available, say nothing rather than fabricate.
    if (typeof structuredClone !== "function") return v;
    try {
      structuredClone(v);
      return v;
    } catch {
      let kind = typeof v;
      try {
        if (v && v.constructor && v.constructor.name) kind = v.constructor.name;
      } catch {
        // An exotic proxy can throw on `.constructor`; `typeof` is still an answer.
      }
      let preview = "";
      try {
        preview = String(v).slice(0, 200);
      } catch {
        preview = "<unstringifiable>";
      }
      return { __unserializable: kind, preview };
    }
  };
  // The job record exists from HERE ON, whatever happens next: `running` is written BEFORE
  // compilation so that a syntactically broken `source` cannot leave start_js with NO record.
  // Invariant: after start_js the record ALWAYS exists (running/error), so poll_job reports
  // `unknown` ONLY on real page-state loss (reload/discard/close/wrong id), never on a
  // compile failure.
  window.__curatorJobs = window.__curatorJobs || {};
  window.__curatorJobs[jobId] = { state: "running" };
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  let fn;
  try {
    // The three compile forms (see the block comment above), now INSIDE this try so the last
    // one throwing on garbage `source` is caught here instead of escaping the function.
    try {
      fn = new AsyncFunction(`return (${source}\n);`);
    } catch (e) {
      if (!(e instanceof SyntaxError)) throw e;
      try {
        fn = new AsyncFunction(`return (${source.replace(/[\s;]+$/, "")}\n);`);
      } catch (e2) {
        if (!(e2 instanceof SyntaxError)) throw e2;
        fn = new AsyncFunction(source);
      }
    }
  } catch (e) {
    // Every form failed to compile: downgrade the job to an HONEST error and return normally.
    // Throwing OUT of the injected function would leave the running record unsettled — and
    // for garbage that never even parses, poll_job must say `error`, not `unknown`.
    window.__curatorJobs[jobId] = { state: "error", message: String((e && e.message) || e) };
    return { jobId };
  }
  (async () => {
    try {
      const v = await fn();
      window.__curatorJobs[jobId] = { state: "done", value: describe(v) };
    } catch (e) {
      window.__curatorJobs[jobId] = { state: "error", message: String((e && e.message) || e) };
    }
  })();
  return { jobId };
}

// The body injected by poll_job. FIXED (takes only the jobId as DATA), so — like get_text —
// no checkbox and no audit row. Reads the record start_js stashed under
// `window.__curatorJobs[jobId]`.
//
// `state:"unknown"` (no global, or no such key) is a DISTINCT, honest signal from
// `"running"`: it means the page-resident state is GONE — the tab reloaded/discarded/closed,
// or the id is wrong — not that the job is still working. The value/message already carry
// describe()'s output from start_js, so they are serializable here.
export function readJobInWorld(jobId) {
  const jobs = typeof window !== "undefined" ? window.__curatorJobs : undefined;
  if (!jobs || !Object.prototype.hasOwnProperty.call(jobs, jobId)) {
    return { state: "unknown" };
  }
  const rec = jobs[jobId] || {};
  const out = { state: typeof rec.state === "string" ? rec.state : "unknown" };
  if ("value" in rec) out.value = rec.value;
  if ("message" in rec) out.message = rec.message;
  return out;
}

// --- the dispatcher ---------------------------------------------------------

// Execute one command frame. `ctx`:
//   - sessionId: the extension's CURRENT session epoch (§5). A frame whose
//     sessionId differs was minted by a dead session and addresses foreign tabs.
//   - now:  () => ms  (injectable clock; defaults to Date.now)
//   - map:  the activity-map module (injectable for tests)
//   - sleep: (ms) => Promise (injectable timer, for the polling verbs — a test that
//     drives wait_for must be able to advance its clock without spending real seconds)
// Returns `{ok, result}` or `{ok, error:{code, message}}`. Never throws — an
// unexpected failure becomes `{ok:false, error:{code:'internal'}}`.
export async function dispatchCommand(frame, ctx = {}) {
  const nowFn = ctx.now || (() => Date.now());
  const map = ctx.map || activityMap;
  const sleep = ctx.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const sessionId = ctx.sessionId;
  if (!frame || typeof frame !== "object") {
    return fail(ERR_INTERNAL, "empty command frame");
  }
  const command = frame.command;
  const params = frame.params || {};

  // SESSION CHECK FIRST (§5): a command from a dead session must NOT execute —
  // its tab ids belong to a session whose tabs this extension no longer owns.
  if (frame.sessionId !== sessionId) {
    return fail(ERR_STALE_SESSION, "command session does not match current session");
  }

  try {
    switch (command) {
      case CMD_OPEN_TAB:
        return await openTab(params, nowFn, map);
      case CMD_CLOSE_TAB:
        return await closeTab(params, nowFn, map);
      case CMD_GET_TAB:
        return Array.isArray(params.items) ? await getTabBulk(params) : await getTab(params);
      case CMD_FOCUS_TAB:
        return await focusTab(params);
      case CMD_FOCUS_WINDOW:
        return await focusWindow(params);
      case CMD_NAVIGATE_TAB:
        return await navigateTab(params, nowFn, sleep);
      case CMD_MERGE_WINDOWS:
        return await mergeWindows(params, nowFn, map);
      case CMD_MOVE_TAB:
        return await moveTab(params, nowFn, map);
      case CMD_EXECUTE_JS:
        return await executeJs(params);
      // The two FIXED-function verbs (§12): no execute_js checkbox, no js_audit row —
      // see the comment above the injected bodies for why that separation is sound.
      case CMD_GET_TEXT:
        return await getText(params);
      case CMD_WAIT_FOR:
        return await waitFor(params, nowFn, sleep);
      // scroll_until and poll_job are FIXED-function verbs too (§12): selectors/direction
      // and a jobId, all DATA — no execute_js checkbox, no js_audit row.
      case CMD_SCROLL_UNTIL:
        return await scrollUntil(params, nowFn, sleep);
      case CMD_POLL_JOB:
        return await pollJob(params);
      // start_js carries ARBITRARY code, so it goes through the same gate as execute_js —
      // the checkbox here at the edge, and the service's js_audit row before the send.
      case CMD_START_JS:
        return await startJs(params);
      // The first chrome.debugger (CDP) verb (§12, wave 18): gated by the SAME single
      // JS & Debugger checkbox as execute_js, but carries no arbitrary code and writes no
      // js_audit row (it only fakes focus, exfiltrating nothing).
      case CMD_SET_FOCUS_EMULATION:
        return await setFocusEmulation(params);
      default:
        return fail(ERR_INTERNAL, `unknown command: ${command}`);
    }
  } catch (e) {
    return fail(ERR_INTERNAL, String((e && e.message) || e));
  }
}

// --- verbs ------------------------------------------------------------------

// §9's window predicate, byte-for-byte the service's own (`_window_mergeable` in
// src/curator/decide.py): type `normal` AND state NOT `fullscreen`. A popup/app/
// devtools window and a fullscreen showcase "are neither folded NOR merged into".
//
// The fullscreen half is not a detail: on macOS a wall dashboard lives as a fullscreen
// window on its own Space (§1, ledger row 43). Letting it be a merge TARGET dumps every
// other window's tabs into the showcase — and a window merge is explicitly NOT undoable
// (§9), so there is nothing to restore the layout from. The same predicate keeps a
// curator-opened copy (open_tab) out of that showcase.
//
// A `maximized` window IS eligible: "не fullscreen" is the spec's wording and the
// service's fork note says the same, so a tab step 4 may relocate never lives in a
// window step 9 refuses to touch.
export function isMergeableWindow(w) {
  return (
    !!w && w.type === "normal" && w.state !== "fullscreen" && w.id !== undefined && w.id !== null
  );
}

// §9's ONE target rule, pure and shared: "обычное окно с наибольшим числом вкладок;
// при равенстве — меньший window_id". Returns null when nothing is eligible.
// `windows` may be the raw getAll() list — the predicate lives here so both callers
// (open_tab's target and merge_windows' target fallback) get the identical answer and
// cannot drift apart.
//
// The tie-break is load-bearing because chrome.windows.getAll() promises no order: two
// equal-sized windows would otherwise be chosen differently on each call, and
// "детерминированно" is exactly the property §9 asks for.
export function pickNormalWindow(windows, tabs) {
  const eligible = (windows || []).filter(isMergeableWindow);
  if (eligible.length === 0) return null;
  const counts = new Map(eligible.map((w) => [w.id, 0]));
  for (const t of tabs || []) {
    if (counts.has(t.windowId)) counts.set(t.windowId, counts.get(t.windowId) + 1);
  }
  let best = null;
  let bestCount = -1;
  for (const w of eligible) {
    const c = counts.get(w.id);
    if (c > bestCount || (c === bestCount && w.id < best)) {
      best = w.id;
      bestCount = c;
    }
  }
  return best;
}

// Create the curator's tab either in `windowId` or — when it is null — in a brand new
// BACKGROUND normal window (§9: on macOS the browser lives with zero windows daily, and
// a background window does not interrupt the human). Returns the created tab.
async function createCuratorTab(params, windowId) {
  if (windowId !== null && windowId !== undefined) {
    return await chrome.tabs.create({
      url: params.url,
      pinned: !!params.pinned,
      active: false, // a curator-opened copy never steals focus
      windowId,
    });
  }
  const win = await chrome.windows.create({
    url: params.url,
    focused: false,
    state: "normal",
  });
  const tab = win && win.tabs && win.tabs[0];
  if (!tab || tab.id === undefined) return null;
  // windows.create takes no `pinned`; apply it to the created tab afterwards.
  if (params.pinned) {
    await chrome.tabs.update(tab.id, { pinned: true });
  }
  return tab;
}

// The live-state wrapper for open_tab. Skips the tab query when nothing is eligible —
// that branch creates a window instead (§9).
async function pickNormalWindowId() {
  const windows = await chrome.windows.getAll();
  if (!windows.some(isMergeableWindow)) return null;
  const tabs = await chrome.tabs.query({});
  return pickNormalWindow(windows, tabs);
}

// open_tab {url, pinned, active:false, seed_age_ms, seed_opened_ago_ms,
// seed_age_unknown}. Validate the scheme at the edge, PICK THE WINDOW
// DETERMINISTICALLY (§9), create the tab, then seed the activity map so the freshly
// opened copy inherits the source's age rather than reading as brand-new. The seed
// races onCreated, but the map's single mutation chain reconciles them.
//
// The explicit windowId is the point (§9): a bare chrome.tabs.create lands in the
// last-focused window, which may be a popup — the copy would then live where the
// pass does not look and phase B would never finish. With ZERO normal windows (on
// macOS the browser lives with none daily) we CREATE one unfocused instead of
// failing: a failure would push the relocation to `deferred` and on to quarantine.
async function openTab(params, nowFn, map) {
  // #49 bulk: an `items` array is the LIST form — the extension loops it item by item
  // (native array forms are fail-fast and report nothing per element) and answers ONE
  // frame carrying a per-item `results` array. See openTabBulk.
  if (Array.isArray(params.items)) {
    return await openTabBulk(params, nowFn, map);
  }
  if (!isHttpUrl(params.url)) {
    return fail(ERR_PRECONDITION_FAILED, "open_tab accepts only http/https urls");
  }
  // #45 "window as address": an explicit `windowId` names the destination. The CALLER
  // chose it (an agent addressing one window), so it is validated with the SAME §9
  // predicate merge_windows/move_tab use, and every miss is a loud refusal — never the
  // auto-select path's "fall back to a window of our own". Without a `windowId` this is
  // exactly today's behaviour: the curator's own pass never names a window, so its
  // deterministic auto-select (and the vanished-window retry) is untouched.
  const named = Number.isInteger(params.windowId);
  let windowId;
  if (named) {
    // Validate the NAMED window live: by command time it may be closed or have become a
    // popup/fullscreen. `no_window` is in the service's _CLIENT_ERRORS set ("your picture
    // is stale, refetch"), so it never lands the copy where the pass cannot see it.
    const windows = await chrome.windows.getAll();
    const target = windows.find((w) => w.id === params.windowId);
    if (!isMergeableWindow(target)) {
      return fail(ERR_NO_WINDOW, `no eligible target window: ${params.windowId}`);
    }
    windowId = params.windowId;
  } else {
    windowId = await pickNormalWindowId();
  }
  let tab;
  try {
    tab = await createCuratorTab(params, windowId);
  } catch (e) {
    const msg = String((e && e.message) || e);
    // Chromium refuses tab edits mid-drag ("Tabs cannot be edited right now (user may
    // be dragging a tab)") — the SAME transient merge_windows classifies below. It is
    // NOT a vanished window, and answering it by opening a window would leave one
    // stray background window per relocation, three per pass, with no self-healing (a
    // one-tab window is never picked again, so the next open repeats it). An honest
    // refusal is cheaper: phase A treats an open failure as "defer, never a strike —
    // the tab retries next pass" (src/curator/phases.py), so nothing is quarantined.
    if (/drag/i.test(msg)) {
      return fail(ERR_BUSY_DRAGGING, msg);
    }
    // The window vanished between getAll() and create(). The response depends on WHO
    // chose it (#45): a window WE auto-selected is retried in one we make ourselves
    // (windows.create cannot lose that race), but a window the CALLER named is refused
    // with `no_window` — silently relocating the tab to some other window would put it
    // where the caller did not ask, and an OLD extension that ignored the key is exactly
    // what the server-side cross-check catches.
    if (named && /no window/i.test(msg)) {
      return fail(ERR_NO_WINDOW, msg);
    }
    if (!named && windowId !== null && /no window/i.test(msg)) {
      tab = await createCuratorTab(params, null);
    } else {
      throw e; // anything else is a genuine fault => internal, which is the truth
    }
  }
  if (!tab || tab.id === undefined) {
    return fail(ERR_NO_WINDOW, "could not create a tab for open_tab");
  }
  await map.seedCuratorTab(tab.id, params, nowFn());
  return ok({ tabId: tab.id, windowId: tab.windowId });
}

// close_tab {tabId, expect:{url, notAudible, notPinned, minIdleMs}}. RE-CHECK the
// volatile guards live (not by the stale snapshot). Any divergence => refuse.
// Only when every guard still holds do we mark the curator cause (AWAITED, so the
// mark is durably written before Chrome activates a neighbour) and remove.
async function closeTab(params, nowFn, map) {
  // #49 bulk: an `items` array is the LIST form — loop item by item, ONE frame back.
  if (Array.isArray(params.items)) {
    return await closeTabBulk(params, nowFn, map);
  }
  const tabId = params.tabId;
  const expect = params.expect || {};

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${tabId}`);
  }

  // url must still match the value the decision was made on.
  if (expect.url !== undefined && tab.url !== expect.url) {
    return fail(ERR_PRECONDITION_FAILED, "url diverged from expect.url");
  }
  // The tab started playing audio after the snapshot (deferred media load in a
  // hidden tab). Do NOT weaken this guard.
  if (expect.notAudible && tab.audible) {
    return fail(ERR_PRECONDITION_FAILED, "tab became audible");
  }
  // The owner pinned it — §8's single "do not touch" signal.
  if (expect.notPinned && tab.pinned) {
    return fail(ERR_PRECONDITION_FAILED, "tab was pinned");
  }
  // The owner came back and is looking at it: active AND in the focused window.
  // (Bare `active` is not enough — an unfocused instance's last-viewed tab is
  // active forever, §5; the pair is the real "being watched" signal.)
  const focused = await chrome.windows.getLastFocused();
  if (tab.active && focused && focused.focused && focused.id === tab.windowId) {
    return fail(ERR_PRECONDITION_FAILED, "tab is active in the focused window");
  }
  // Idle age fell below the threshold (the owner re-viewed a tab that was idle at
  // snapshot time). Checked against the OWN activity map. A missing record means
  // we can no longer prove idleness => refuse conservatively.
  if (typeof expect.minIdleMs === "number" && expect.minIdleMs > 0) {
    const m = await map.readMap();
    const rec = m.tabs && m.tabs[tabId];
    const idleMs = rec ? nowFn() - rec.lastActive : -1;
    if (idleMs < expect.minIdleMs) {
      return fail(ERR_PRECONDITION_FAILED, "idle age below minIdleMs");
    }
  }

  // All guards hold. Write the curator cause BEFORE removing and AWAIT it (§6):
  // the close makes Chrome activate a neighbour, and the pre-written mark
  // suppresses that neighbour's onActivated from counting as activity.
  await map.markCuratorCause(tab.windowId, nowFn());
  try {
    await chrome.tabs.remove(tabId);
  } catch (e) {
    // The remove failed => undo the mark so a later real activation still counts.
    await map.clearCuratorCause(tab.windowId);
    return fail(ERR_INTERNAL, `remove failed: ${String((e && e.message) || e)}`);
  }
  return ok({ ok: true });
}

// get_tab {tabId} -> {tab} or no_such_tab.
async function getTab(params) {
  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  return ok({ tab });
}

// get_tab {items:[{tabId}]} -> {results:[{index, ok, tabId, error?}]}. The bulk copy-check
// for #49 relocate: each item is its OWN chrome.tabs.get in a try/catch, so one gone tab
// (ERR_NO_SUCH_TAB) does not sink the whole frame — a present copy answers ok:true, a
// vanished one ok:false + error, matched back by index. No mutation, purely a read.
async function getTabBulk(params) {
  const items = params.items || [];
  const results = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index] || {};
    const tabId = item.tabId;
    try {
      await chrome.tabs.get(tabId);
      results.push({ index, ok: true, tabId });
    } catch {
      results.push({ index, ok: false, tabId, error: ERR_NO_SUCH_TAB });
    }
  }
  return ok({ results });
}

// focus_tab {tabId} -> activate the tab and focus its window. The resulting
// onActivated stamp counting as activity is BY DESIGN: focus_tab exists to put a
// tab in front of the human (the startpage "jump", §10), so it genuinely IS a
// view — it should reset the idle clock.
async function focusTab(params) {
  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  await chrome.tabs.update(params.tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return ok({ ok: true });
}

// focus_window {windowId} -> raise the window to the foreground, changing NOTHING
// inside it. Unlike focus_tab this NEVER touches a tab (no chrome.tabs.update): the
// startpage "click a space" jump wants the browser in front without activating any
// tab, so the window's own active tab and idle clocks stay exactly as they were. A
// window that vanished between the mirror snapshot and the command is `no_window`,
// which the service maps to a 409 refetch (§10) — the same "your picture is stale"
// signal focus_tab's no_such_tab carries.
async function focusWindow(params) {
  try {
    await chrome.windows.get(params.windowId);
  } catch {
    return fail(ERR_NO_WINDOW, `no such window: ${params.windowId}`);
  }
  await chrome.windows.update(params.windowId, { focused: true });
  return ok({ ok: true });
}

// navigate_tab {tabId, url}. Validate the scheme at the edge (same anti-smuggle
// rule as open_tab), then point the tab at the url.
//
// NOTE (§7/§5): the resulting onUpdated document-change stamps the tab's
// lastActive, so a curator navigation reads as activity and refreshes the idle
// clock. This is accepted BY DESIGN for now: it errs SAFE (a too-fresh tab is
// never wrongly closed; it self-heals within IDLE_MINUTES), navigate_tab has no
// curator caller yet (reset defers the content change; MCP is a later phase), and
// per-tab curator-nav suppression is a new mechanism best added with its first
// real caller. curatorCause is per-WINDOW (for a close/move neighbour activation),
// which cannot express "suppress this one tab's navigation".
//
// `waitUntil` (§6, optional) turns the fire-and-forget navigation into one that reports
// when the page is actually there. DEFAULT `'none'` is today's behaviour byte for byte —
// same single `tabs.update`, same bare `{ok:true}` — because the reset path
// (src/api/rules.py) calls this verb and must not change at all.
//
//   'none'     — return as soon as the update is issued (today).
//   'load'     — poll until the tab reports `status === 'complete'`.
//   'selector' — poll until `selector` matches in the page (needs `selector`).
//
// A wait that reaches its deadline is a SUCCESS carrying `matched:false` — never an error.
// The tab was pointed at the url either way, so "the condition never came true" is a
// definite answer; `timeout` is reserved for the service's "no frame arrived at all"
// (see wait_for's header comment for the full argument).
async function navigateTab(params, nowFn, sleep) {
  if (!isHttpUrl(params.url)) {
    return fail(ERR_PRECONDITION_FAILED, "navigate_tab accepts only http/https urls");
  }
  const waitUntil =
    params.waitUntil === undefined || params.waitUntil === null ? "none" : params.waitUntil;
  if (waitUntil !== "none" && waitUntil !== "load" && waitUntil !== "selector") {
    return fail(
      ERR_PRECONDITION_FAILED,
      `navigate_tab waitUntil must be one of none/load/selector (got ${JSON.stringify(waitUntil)})`,
    );
  }
  if (waitUntil === "selector" && (typeof params.selector !== "string" || params.selector === "")) {
    return fail(
      ERR_PRECONDITION_FAILED,
      "navigate_tab waitUntil:'selector' requires a non-empty selector",
    );
  }
  // VALIDATE BEFORE NAVIGATING: a bad waitUntil/selector must not leave the tab pointed
  // somewhere new and then refuse — the refusal would read as "nothing happened".
  const deadlineMs = waitUntil === "none" ? 0 : clampWaitMs(params.timeoutMs);
  if (waitUntil !== "none" && deadlineMs === null) {
    return fail(ERR_PRECONDITION_FAILED, "navigate_tab timeoutMs must be a positive integer");
  }
  // The url AND the status the tab is leaving, read BEFORE the update — the only markers
  // that tell the OLD document from the new one (see `navigationCommitted`; the status is
  // what makes its grace bound safe). Read for both waiting modes, never for 'none', so the
  // default path puts exactly the calls on the browser it always did.
  let startUrl = null;
  let startStatus = null;
  if (waitUntil !== "none") {
    try {
      const before = await chrome.tabs.get(params.tabId);
      startUrl = (before && before.url) || null;
      startStatus = (before && before.status) || null;
    } catch {
      return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
    }
  }
  try {
    await chrome.tabs.update(params.tabId, { url: params.url });
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  if (waitUntil === "none") return ok({ ok: true });

  const started = nowFn();
  const deadline = started + deadlineMs;
  // ONE poll interval before the first check, deliberately. Right after `tabs.update` the
  // tab can still report the OLD page's `status:'complete'` for a tick — accepting that
  // would make `waitUntil:'load'` return before the new document has even started
  // loading, i.e. exactly the bug the option exists to prevent. Costs one interval.
  await sleep(Math.min(WAIT_POLL_MS, deadlineMs));
  // GATE BOTH WAITING MODES ON THE COMMIT, inside the same deadline. Until the new document
  // commits, 'selector' would inject into the OLD one and 'load' would read the OLD one's
  // `status:'complete'` — the same defect answered twice, so it gets one answer. See
  // `navigationCommitted` for the signals and for what the gate costs. A commit that never
  // arrives ends the same way a condition that never becomes true does: `matched:false`,
  // because the update WAS issued and that is a verdict.
  const commit = await pollUntil(
    navigationCommitted(params.tabId, params.url, startUrl, startStatus),
    deadline,
    nowFn,
    sleep,
    params.tabId,
  );
  if (commit.error) return fail(commit.error, commit.message);
  if (!commit.matched) {
    return ok({ ok: true, matched: false, elapsedMs: nowFn() - started });
  }
  const probe =
    waitUntil === "load"
      ? async () => {
          const tab = await chrome.tabs.get(params.tabId);
          return tab && tab.status === "complete";
        }
      : injectingProbe(params.tabId, params.selector, undefined, "navigate_tab");
  const outcome = await pollUntil(probe, deadline, nowFn, sleep, params.tabId);
  if (outcome.error) return fail(outcome.error, outcome.message);
  // `matched:false` is a SUCCESS carrying a negative verdict, exactly as in wait_for: the
  // navigation WAS issued, and "the condition never became true" is a definite answer, not
  // the "no frame arrived / state unknown" that `timeout` reserves for the service.
  return ok({ ok: true, matched: !!outcome.matched, elapsedMs: nowFn() - started });
}

// --- get_text / wait_for: FIXED-function observation (§12) -------------------

// Clamp a caller's timeout to the extension's own ceiling. Returns null for a value that
// is not a positive integer, so the caller can refuse it loudly rather than invent one.
function clampWaitMs(value) {
  if (!Number.isInteger(value) || value <= 0) return null;
  return Math.min(value, WAIT_MAX_TIMEOUT_MS);
}

// Run the FIXED predicate in the page. Returns `true`/`false`, or a `{fatal}` sentinel for
// a selector that does not parse — see `pollUntil` for why that cannot be a throw.
async function injectMatch(tabId, selector, textContains) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: matchInWorld,
    args: [selector ?? null, textContains ?? null],
  });
  const got = ((results || [])[0] || {}).result || {};
  if (got.badSelector) {
    return {
      fatal: {
        code: ERR_PRECONDITION_FAILED,
        message: `invalid CSS selector ${JSON.stringify(selector)}: ${got.message}`,
      },
    };
  }
  return !!got.matched;
}

// Wrap an injecting predicate so the target's SCHEME is re-checked on EVERY poll.
//
// Checking it once up front is not enough for a call that keeps injecting for up to a
// minute: the page can move under us (a redirect, a user click) onto a `file://` url, and
// with <all_urls> granted the next injection would read that local page same-origin. A
// guard that expires mid-wait is not a guard.
function injectingProbe(tabId, selector, textContains, verb) {
  return async () => {
    const live = await chrome.tabs.get(tabId);
    if (!isHttpUrl(live && live.url)) {
      return {
        fatal: {
          code: ERR_PRECONDITION_FAILED,
          message: `${verb} target is not an http/https tab`,
        },
      };
    }
    return await injectMatch(tabId, selector, textContains);
  };
}

// A probe that answers "the navigation this command issued has COMMITTED" — the gate BOTH
// of navigate_tab's waiting modes run before they start testing their own condition.
//
// `chrome.tabs.update(tabId, {url})` does NOT move `tab.url`: until the new document
// commits, `tabs.get` keeps answering the PREVIOUS url and the target sits in
// `tab.pendingUrl`. Injecting inside that window reads the OLD DOM, and that produced two
// distinct wrong answers: a selector that exists on both pages (`#app`, `body`, an SPA
// header) matched at once — so `waitUntil` waited for nothing — and a tab leaving a
// non-http page (`about:blank`, a `chrome://` newtab, `file://`) tripped the per-poll
// scheme guard and answered "navigate_tab target is not an http/https tab": an error about
// something that did not happen, AFTER the navigation was already issued. The single
// pre-probe pause cannot cover either: the commit lands after the server answers, so
// 250 ms is a heuristic, not a guarantee.
//
// `waitUntil:'load'` is gated for the SAME reason and not by symmetry. `status` is meant to
// flip to `loading` when the navigation STARTS, but a `beforeunload` dialog, a throttled
// background tab or a busy MV3 worker delays that flip past our first poll — and until it
// happens, `complete` describes the page being LEFT. One gate, both modes.
//
// Three signals, in the order they are trustworthy:
//   1. `pendingUrl` is set => still in flight, never commit. (It needs the `tabs`
//      permission, which the manifest grants, so this is the normal case, not a fallback.)
//   2. the tab's url IS the target, or it simply LEFT `startUrl` — a redirect landing
//      elsewhere is still the new document, which is what "commit" has to mean here.
//      ⚠️ Leaving `startUrl` is not PROOF of a new document: a `#fragment` target, or a
//      `pushState` from the page being left, moves the url same-document and opens the
//      gate on the OLD DOM. Same escape as the 204/attachment case, different signal.
//   3. the tab went `loading` and came back `complete` — for a navigation whose url can
//      never satisfy (2), e.g. a redirect chain that lands back on the address we started
//      from. Least precise of the three, hence last, and reachable only after (1) cleared.
//      It ALSO requires the tab not to have been `loading` before the update: see the
//      paragraph on that at the bottom, which is where this signal's own wrong answer lived.
//
// ⚠️ Navigating a tab to the address it is ALREADY on rests on (1) alone: the moment
// `pendingUrl` clears, (2) holds by definition, and (3) is never reached. Where the field
// is unavailable the gate would open on the pre-reload document — accepted deliberately,
// because there the old document IS the same page, and the alternative (demand (3)'s
// loading→complete round) answers `matched:false` about a reload short enough to fit
// between two polls.
//
// AND A BOUNDED GRACE, because those three leave a hole that none of them can close. A
// navigation whose FINAL address is `startUrl` — a redirect that bounces back, `/admin`
// refused throwing the tab to `/login` — can never satisfy (2); and if it also commits and
// finishes inside the pre-pause (cache, a local redirect, a 304), no poll ever observes
// `loading`, so (3) is dead too. The gate would then stay shut for the WHOLE deadline and
// answer `matched:false` about a page that is loaded and does contain the selector — where
// the same call answered `matched:true` in 250 ms before the gate existed. So after
// WAIT_COMMIT_GRACE_POLLS CONSECUTIVE polls in which nothing suggested a navigation at all
// — no `pendingUrl`, no `loading`, the url still `startUrl`, and the tab already `complete`
// BEFORE the update, so this `complete` cannot be the old page still finishing — the gate
// opens anyway.
//
// WHAT THE GRACE ASSUMES — written down so a later reader can attack the assumption instead
// of guessing it: that a navigation which has really STARTED exposes `pendingUrl` within one
// `tabs.get`. That is deliberately NOT the claim that `status` flips promptly; the paragraph
// gating 'load' above says the opposite about `status`, and the two only look opposed.
// `pendingUrl` is set by the navigation controller when the request is issued, while the
// `loading` flip is what `beforeunload`, a throttled tab or a busy worker delay. So the
// grace leans on the OTHER signal, the one that argument does NOT call unreliable — which is
// why the two can both be true. Falsify it — a browser that leaves `pendingUrl` empty while
// a load is in flight — and the grace starts firing on live navigations; then the bound has
// to grow, or this signal has to change.
//
// IT IS NOT CONFINED TO THE ⚠️ CORNER ABOVE, and pretending otherwise would be the comment
// lying about its own cost. "Three polls with no evidence" requires NEITHER that the
// addresses match NOR that the document be unchanged: start `https://old/`, target
// `https://new/`, a navigation simply not visible yet, and the answer is `matched:true`
// about `https://old/` with the tab still on the old address. That is a THIRD way to be
// answered about the document being left, next to the two the MCP description names (a
// same-address reload; a navigation that changes no document at all). It is the deliberate
// trade, not an oversight: a rare wrong document instead of a CERTAIN wrong answer after the
// full deadline. Narrowing it — demanding `targetUrl === startUrl` — would close this third
// escape and reopen the very stall the grace exists for, since a redirect that lands back on
// `startUrl` has a TARGET that differs. The BOUND is what keeps it rare rather than normal:
// a navigation that really started needs one `tabs.get` to show `pendingUrl` or `loading`,
// not three, so anything still silent on the third poll is a navigation we have no evidence
// of at all.
//
// IT DOES NOT APPLY TO A NON-HTTP(S) START, and that narrowing is exactly the shape of the
// hole. Every case the grace exists for — a redirect bouncing back to `startUrl`, a cache
// hit, a 304 — presupposes an http(s) document to bounce back TO: a tab sitting on
// `about:blank` cannot "redirect back to about:blank". So on a non-http start the grace
// bought nothing and re-opened precisely the two wrong answers this gate was written to
// prevent — `precondition_failed: not an http/https tab` for 'selector' (an error about a
// navigation that DID happen) and `matched:true` about `about:blank` for 'load'. No
// injection ever reached the non-http document (the per-poll scheme guard held), so what it
// cost was wrong ANSWERS, not access.
//
// A TAB ALREADY `loading` GETS NEITHER (3) NOR THE GRACE, and the (3) half is the one that
// was actually wrong rather than merely missing. Such a tab produces a `loading` →
// `complete` round ALL BY ITSELF, out of the document it is LEAVING — so (3) used to read
// that as the commit, and it did so TWO POLLS EARLIER than the grace it is denied would
// have. Reproduced: a mid-load tab whose flip to `loading` and whose `pendingUrl` are both
// delayed past our polls (a `beforeunload` dialog, a throttled tab, a busy MV3 worker — the
// same three reasons the 'load' gate exists at all), the old page landing on poll 2, and the
// answer `matched:true` at 500 ms about `https://old/`, with the selector probe injected
// into the OLD DOM. That is the precise defect this whole gate exists to remove, so the
// `startStatus !== "loading"` term closes it. Pinned by "...and gets no SIGNAL (3) either"
// in commands.test.js — the older "gets NO grace" test cannot see it, because there the
// commit lands on the first poll and `sawLoading` never turns true.
//
// WHAT REFUSING IT COSTS, written down rather than discovered later: for a tab that was
// mid-load, a navigation whose FINAL address is the one it started from — the redirect that
// bounces back, `/admin` refused throwing the tab to `/login` and back — now has NO signal
// left at all. (2) is unsatisfiable by definition, (3) is refused here, the grace was
// refused above; the navigation really happened, and the answer is `matched:false` at the
// deadline. That is the trade this codebase keeps making: an agent that knows it does not
// know beats one told confidently about the page it has just left. Two things are NOT part
// of that cost — navigating a mid-load tab to the address it is ALREADY on (there (2) holds
// the moment `pendingUrl` clears, the ⚠️ corner above), and a `startStatus` of `null` or
// `unloaded` (the term claims "not `loading`", and an unknown or discarded status is not a
// known `loading`; only a `complete` start earns the GRACE, which is the stricter test).
function navigationCommitted(tabId, targetUrl, startUrl, startStatus) {
  let sawLoading = false;
  let quietPolls = 0;
  return async () => {
    const tab = await chrome.tabs.get(tabId);
    if (!tab) return false;
    if (tab.status === "loading") sawLoading = true;
    if (tab.pendingUrl) {
      quietPolls = 0; // a navigation IS in flight — the opposite of "no evidence"
      return false;
    }
    if (tab.url === targetUrl || (startUrl !== null && tab.url !== startUrl)) return true;
    // (3), and the `startStatus` term is load-bearing: for a tab that was ALREADY `loading`
    // when the update was issued, a `complete` seen now is the OLD document finishing. See
    // "A TAB ALREADY `loading` GETS NEITHER (3) NOR THE GRACE" above for the reproduction
    // and for what refusing it costs.
    if (sawLoading && tab.status === "complete" && startStatus !== "loading") return true;
    const quiet =
      // REDUNDANT BY CONSTRUCTION, and kept anyway: the only way to reach this line with
      // `sawLoading` true and the tab `complete` is a `startStatus` of `loading` — which the
      // very next term rejects. (Before that term existed the return just above did the same
      // job on its own.) So no mutation of this one can redden a test; it is insurance
      // against these lines being reordered, not a load-bearing check.
      !sawLoading &&
      startStatus === "complete" &&
      tab.status === "complete" &&
      tab.url === startUrl &&
      // See "IT DOES NOT APPLY TO A NON-HTTP(S) START" above: no non-http page can be the
      // page a redirect bounces back to, so the grace would buy nothing and cost two wrong
      // answers. Pinned by "a non-http START gets NO grace" in commands.test.js.
      isHttpUrl(startUrl);
    quietPolls = quiet ? quietPolls + 1 : 0;
    return quietPolls >= WAIT_COMMIT_GRACE_POLLS;
  };
}

// Poll `probe` every WAIT_POLL_MS until it answers true or `deadline` passes.
//
// An injection that THROWS is not a failure here: mid-navigation Chromium tears the frame
// down and answers "Frame with ID 0 was removed" — which is precisely the moment we are
// waiting through. So a throw is swallowed and we re-test whether the TAB still exists;
// only a vanished tab ends the wait (waiting for a condition in a closed tab can never
// succeed, and silently burning the whole budget would hide the real cause).
//
// That leniency is also why a probe reports a CALLER error as a `{fatal}` VALUE instead of
// throwing: a throw would be read as "the frame is being recreated" and polled through to
// the deadline, turning a typo into a minute of waiting and then the wrong verdict.
//
// Returns `{matched:true}` / `{matched:false}` (deadline) / `{error, message}`.
async function pollUntil(probe, deadline, nowFn, sleep, tabId) {
  for (;;) {
    try {
      const got = await probe();
      // Order matters: `{fatal}` is truthy, so it must be tested before the plain "true".
      if (got && got.fatal) return { error: got.fatal.code, message: got.fatal.message };
      if (got) return { matched: true };
    } catch (e) {
      try {
        await chrome.tabs.get(tabId);
      } catch {
        return { error: ERR_NO_SUCH_TAB, message: `no such tab: ${tabId}` };
      }
      void e; // transient: the frame was being replaced. Keep polling.
    }
    const remaining = deadline - nowFn();
    if (remaining <= 0) return { matched: false };
    await sleep(Math.min(WAIT_POLL_MS, remaining));
  }
}

// get_text {tabId, selector?, maxBytes?} -> {text, truncated?, totalBytes?}.
//
// NOT gated by the execute_js checkbox and writing NO js_audit row: the injected function
// is fixed and committed (see the injected-bodies comment). The http/https edge guard IS
// applied, exactly as execute_js applies it — with <all_urls> granted, an unguarded target
// would read a `file://` page's text same-origin.
async function getText(params) {
  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  if (!isHttpUrl(tab.url)) {
    return fail(ERR_PRECONDITION_FAILED, "get_text target is not an http/https tab");
  }
  const selector = typeof params.selector === "string" && params.selector !== "" ? params.selector : null;
  const maxBytes = Number.isInteger(params.maxBytes) && params.maxBytes > 0 ? params.maxBytes : null;
  const results = await chrome.scripting.executeScript({
    target: { tabId: params.tabId },
    func: readTextInWorld,
    args: [selector, maxBytes],
  });
  const got = ((results || [])[0] || {}).result || {};
  if (got.badSelector) {
    // "Your selector does not parse" — distinct from "it parsed and matched nothing", and
    // from `internal`, which is what this used to become by escaping the injection.
    return fail(
      ERR_PRECONDITION_FAILED,
      `invalid CSS selector ${JSON.stringify(selector)}: ${got.message}`,
    );
  }
  if (!got.found) {
    // A selector that matched nothing is a REFUSAL, not an empty page (see
    // readTextInWorld). With no selector this means the document has no body at all.
    return fail(
      ERR_PRECONDITION_FAILED,
      selector
        ? `selector ${selector} matched no element in tab ${params.tabId}`
        : `tab ${params.tabId} has no document body to read`,
    );
  }
  const result = { text: got.text ?? "" };
  if (got.truncated) {
    result.truncated = true;
    // camelCase on the WIRE like every other §6 key (`tabId`, `elapsedMs`); the MCP layer
    // is the one that renames it to snake_case for the agent.
    result.totalBytes = got.totalBytes;
  }
  return ok(result);
}

// wait_for {tabId, urlMatches?|selector?|textContains?, timeoutMs} ->
// {matched:boolean, elapsedMs}.
//
// A DEADLINE THAT PASSES IS A SUCCESS, not an error, and this is the whole shape of the
// verb. §11 fixes `timeout` to mean UNKNOWN — no frame arrived, the browser may be wedged,
// do not blindly retry. A wait that ran to its deadline is the opposite: the browser is
// alive, the frame DID arrive, and the answer "no, it never became true" is a definite
// negative the agent can act on. Spelling both as `timeout` would ask the agent to tell
// them apart from a string; spelling this one as `{ok:true, matched:false}` puts the
// distinction in the response SHAPE, which survives the wire. `timeout` therefore has
// exactly ONE producer again: the service, when nothing came back.
//
// EXACTLY ONE predicate. Zero or several is `precondition_failed` and NOTHING is polled:
// "wait for A and B" and "wait for A or B" are different verbs, and guessing which one
// the caller meant would make a 30-second wait answer a question nobody asked.
//
// `urlMatches` is a substring test against the live `chrome.tabs.get(...).url` and needs
// NO injection at all — which is also why it carries no http/https guard: the usual wait
// is precisely for a tab to REACH an http url, and a tab mid-navigation legitimately sits
// on `about:blank` for a moment. The two injecting predicates DO carry the guard, up front
// AND on every poll (see `injectingProbe`): an agent waiting for a selector in a `file://`
// tab has made a mistake it should hear about immediately, not 30 seconds later.
async function waitFor(params, nowFn, sleep) {
  const keys = ["urlMatches", "selector", "textContains"].filter(
    (k) => params[k] !== undefined && params[k] !== null,
  );
  if (keys.length !== 1) {
    return fail(
      ERR_PRECONDITION_FAILED,
      `wait_for requires EXACTLY ONE of urlMatches / selector / textContains (got ${keys.length})`,
    );
  }
  const key = keys[0];
  const needle = params[key];
  if (typeof needle !== "string" || needle === "") {
    return fail(ERR_PRECONDITION_FAILED, `wait_for ${key} must be a non-empty string`);
  }
  const budget = clampWaitMs(params.timeoutMs);
  if (budget === null) {
    return fail(ERR_PRECONDITION_FAILED, "wait_for timeoutMs must be a positive integer");
  }

  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  if (key !== "urlMatches" && !isHttpUrl(tab.url)) {
    return fail(ERR_PRECONDITION_FAILED, "wait_for target is not an http/https tab");
  }

  const started = nowFn();
  const deadline = started + budget;
  const probe =
    key === "urlMatches"
      ? async () => {
          const live = await chrome.tabs.get(params.tabId);
          return !!(live && typeof live.url === "string" && live.url.includes(needle));
        }
      : injectingProbe(
          params.tabId,
          key === "selector" ? needle : null,
          key === "textContains" ? needle : null,
          "wait_for",
        );
  const outcome = await pollUntil(probe, deadline, nowFn, sleep, params.tabId);
  if (outcome.error) return fail(outcome.error, outcome.message);
  // Both verdicts are `ok` — see the header comment. `matched:false` says the deadline
  // passed with the condition still false; it does NOT say the browser failed to answer.
  return ok({ matched: !!outcome.matched, elapsedMs: nowFn() - started });
}

// scroll_until {tabId, countSelector, containerSelector?, direction?, targetCount?,
// stableRounds?, intervalMs?, timeoutMs, focus?} -> {count, rounds, stopped, elapsedMs}.
//
// FIXED-function (see scrollAndCountInWorld): NOT behind the execute_js checkbox, writes no
// js_audit row. The scheduler lives HERE in the worker — each step is a fresh
// chrome.scripting inject with `await sleep(intervalMs)` between steps — so its pacing does
// not depend on the page's own (throttleable) timers, the same reason wait_for polls here.
//
// Guards: no_such_tab and the http/https edge check up front like get_text, PLUS a
// per-step existence AND scheme re-check (the pollUntil pattern) because the loop keeps
// injecting for up to a minute and a page can move under us onto a file:// url — a guard
// that expires mid-scroll is not a guard.
async function scrollUntil(params, nowFn, sleep) {
  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  if (!isHttpUrl(tab.url)) {
    return fail(ERR_PRECONDITION_FAILED, "scroll_until target is not an http/https tab");
  }
  const countSelector =
    typeof params.countSelector === "string" && params.countSelector !== "" ? params.countSelector : null;
  if (countSelector === null) {
    return fail(ERR_PRECONDITION_FAILED, "scroll_until requires a non-empty countSelector");
  }
  const containerSelector =
    typeof params.containerSelector === "string" && params.containerSelector !== ""
      ? params.containerSelector
      : null;
  const direction = params.direction === "up" ? "up" : "down";
  const budget = clampWaitMs(params.timeoutMs);
  if (budget === null) {
    return fail(ERR_PRECONDITION_FAILED, "scroll_until timeoutMs must be a positive integer");
  }
  // Floors: a non-positive interval would spin, a non-positive stableRounds would stop
  // before the first non-growing step is even observed. The MCP layer supplies defaults, so
  // these only backstop a hand-crafted frame.
  const intervalMs = Number.isInteger(params.intervalMs) && params.intervalMs > 0 ? params.intervalMs : 700;
  const stableRounds =
    Number.isInteger(params.stableRounds) && params.stableRounds > 0 ? params.stableRounds : 3;
  const targetCount = Number.isInteger(params.targetCount) && params.targetCount > 0 ? params.targetCount : null;

  // focus: hand the SCREEN to this tab before the loop. Documented cost (§16): feeds built
  // on IntersectionObserver do not fire in a BACKGROUND tab (the IO never intersects a tab
  // that is not rendered — confirmed by measurement), so their content never loads and the
  // count never grows. Focusing fixes that but takes the screen away from the human, which
  // is exactly why it is an explicit opt-in rather than the default.
  if (params.focus) {
    await chrome.tabs.update(params.tabId, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  }

  const started = nowFn();
  const deadline = started + budget;
  let count = 0;
  let rounds = 0;
  let stable = 0;
  let stopped = "deadline";
  for (;;) {
    let live;
    try {
      live = await chrome.tabs.get(params.tabId);
    } catch {
      return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
    }
    if (!isHttpUrl(live && live.url)) {
      return fail(ERR_PRECONDITION_FAILED, "scroll_until target is not an http/https tab");
    }
    let results;
    try {
      results = await chrome.scripting.executeScript({
        target: { tabId: params.tabId },
        func: scrollAndCountInWorld,
        args: [containerSelector, direction, countSelector],
      });
    } catch (e) {
      // A thrown inject mid-scroll means the frame is being replaced (a navigation, a
      // discard) — the tab still exists (checked above), so treat it as a transient step:
      // wait and retry rather than counting it as "no growth" or dying as `internal`. The
      // deadline still bounds the loop.
      void e;
      const remainingT = deadline - nowFn();
      if (remainingT <= 0) {
        stopped = "deadline";
        break;
      }
      await sleep(Math.min(intervalMs, remainingT));
      continue;
    }
    const got = ((results || [])[0] || {}).result || {};
    if (got.badSelector) {
      return fail(ERR_PRECONDITION_FAILED, `invalid CSS selector: ${got.message}`);
    }
    if (got.noContainer) {
      return fail(
        ERR_PRECONDITION_FAILED,
        `scroll_until container ${containerSelector} matched no element in tab ${params.tabId}`,
      );
    }
    const newCount = Number.isInteger(got.count) ? got.count : 0;
    rounds += 1;
    // Growth resets the stability counter; a step that did not grow increments it.
    if (newCount > count) stable = 0;
    else stable += 1;
    count = newCount;
    // target first: reaching the wanted count is a success even on the step it grew to it.
    if (targetCount !== null && count >= targetCount) {
      stopped = "target";
      break;
    }
    if (stable >= stableRounds) {
      stopped = "stable";
      break;
    }
    const remaining = deadline - nowFn();
    if (remaining <= 0) {
      stopped = "deadline";
      break;
    }
    await sleep(Math.min(intervalMs, remaining));
  }
  return ok({ count, rounds, stopped, elapsedMs: nowFn() - started });
}

// start_js {tabId, code, world?, jobId} -> {jobId}. ARBITRARY code, so gated on the
// execute_js checkbox HERE at the edge (read FRESH, default OFF) exactly like execute_js —
// while the service writes the js_audit row BEFORE the
// send (§12). The injected startJobInWorld wraps the code fire-and-forget and answers
// {jobId} at once; the promise runs on in the page. `jobId` is minted by the service and
// echoed back so the caller can poll it.
async function startJs(params) {
  const stored = await chrome.storage.local.get(ALLOW_EXECUTE_JS_KEY);
  const allowed = !!(stored && stored[ALLOW_EXECUTE_JS_KEY]);
  if (!allowed) {
    return fail(ERR_JS_DISABLED, "start_js is disabled in this copy's options");
  }
  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  if (!isHttpUrl(tab.url)) {
    return fail(ERR_PRECONDITION_FAILED, "start_js target is not an http/https tab");
  }
  const jobId = String(params.jobId == null ? "" : params.jobId);
  const results = await chrome.scripting.executeScript({
    target: { tabId: params.tabId },
    world: params.world || "MAIN",
    func: startJobInWorld,
    args: [jobId, String(params.code == null ? "" : params.code)],
  });
  const got = ((results || [])[0] || {}).result || {};
  return ok({ jobId: got.jobId || jobId });
}

// poll_job {tabId, jobId, world?} -> {state, value?, message?}. FIXED read (readJobInWorld),
// so no execute_js checkbox and no js_audit row; the http/https edge guard still applies
// like get_text. Injects into MAIN by default — the world start_js runs in unless told
// otherwise — so the job global it reads is the one start_js wrote. `state:"unknown"` means
// the page-resident record is gone (reload/discard/close, or a wrong id), distinct from
// `"running"`.
async function pollJob(params) {
  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  if (!isHttpUrl(tab.url)) {
    return fail(ERR_PRECONDITION_FAILED, "poll_job target is not an http/https tab");
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId: params.tabId },
    world: params.world || "MAIN",
    func: readJobInWorld,
    args: [String(params.jobId == null ? "" : params.jobId)],
  });
  const got = ((results || [])[0] || {}).result || {};
  const result = { state: typeof got.state === "string" ? got.state : "unknown" };
  if ("value" in got) result.value = got.value;
  if ("message" in got) result.message = got.message;
  return ok(result);
}

// merge_windows {windowIds?, targetWindowId?}. Move the source windows' tabs into
// the target window. Mark BOTH the source and target windows BEFORE moving (a
// move activates a neighbour in the emptied source and re-activates in the
// target). Empty params = the manual "merge all" button (§9): every other normal
// window folds into the focused normal one, or — with nothing normal focused — into
// the deterministic §9 window (see the target block below). A move rejected because
// the user is dragging a tab surfaces as busy_dragging.
async function mergeWindows(params, nowFn, map) {
  let targetWindowId = params.targetWindowId;
  let windowIds = params.windowIds;

  const allWindows = await chrome.windows.getAll();
  // §9's mergeable set — the ONE predicate for BOTH roles (source and target):
  // `isMergeableWindow` = normal && not fullscreen, the service's own
  // `_window_mergeable`. getAll()/getLastFocused() also report popup/app windows and a
  // fullscreen showcase; none of them may be folded, and none may be merged INTO.
  const mergeable = allWindows.filter(isMergeableWindow);
  const mergeableIds = new Set(mergeable.map((w) => w.id));
  // ONE live read of the tab list and of the focused window, serving BOTH the target
  // choice and the edge re-check further down. Two separate getLastFocused calls could
  // disagree with each other inside a single command.
  const tabs = await chrome.tabs.query({});
  const focusedNow = await chrome.windows.getLastFocused();
  // On-screen window id (for the volatile "the owner is looking at it" guard): ANY
  // normal window counts here, fullscreen included — it is very much on screen.
  const onScreenId =
    focusedNow &&
    focusedNow.type === "normal" &&
    focusedNow.id !== undefined &&
    focusedNow.id !== -1
      ? focusedNow.id
      : null;

  if (targetWindowId === undefined || targetWindowId === null) {
    // Empty params = the manual "слить всё сейчас" button (§9). TWO criteria, in order:
    //
    //  1. The focused window IF it is mergeable, and that is not cosmetic. The edge
    //     re-check below refuses to move a window whose active tab is on screen, so a
    //     focused window used as a SOURCE would simply be dropped — the button would
    //     leave unmerged exactly the window the human is looking at. As the TARGET it
    //     is never dropped: idle sources fold INTO the window in use. The mergeability
    //     test is what stops focus from smuggling a FULLSCREEN showcase in as the
    //     destination of an irreversible merge.
    //  2. Otherwise §9's stated rule — "обычное окно с наибольшим числом вкладок; при
    //     равенстве — меньший window_id" — via the SAME helper open_tab uses. The old
    //     `normalWindows[0]` was not that rule and not deterministic at all:
    //     chrome.windows.getAll() promises no order, so the target flipped between
    //     calls and the fewest-moves property §9 buys with "наибольшее число вкладок"
    //     was lost.
    //
    // `??` (not `||`): windowId 0 is a legal id that `||` would discard.
    const focusedTargetId = onScreenId !== null && mergeableIds.has(onScreenId) ? onScreenId : null;
    targetWindowId = focusedTargetId ?? pickNormalWindow(mergeable, tabs);
  }
  // VALIDATE the target, however it was chosen — including one the SERVICE named. It
  // decided on the step-3 snapshot and a pass runs for minutes: by command time that
  // window may be closed, or have become a popup/fullscreen. Without this check
  // chrome.tabs.move throws and the command answers `internal`, which the service maps
  // to 502 — while `no_window` is in its _CLIENT_ERRORS set (src/api/instances.py) and
  // comes back as 409 + refetch, i.e. "your picture of the windows is stale, re-read it".
  if (
    targetWindowId === undefined ||
    targetWindowId === null ||
    !mergeableIds.has(targetWindowId)
  ) {
    return fail(ERR_NO_WINDOW, "no mergeable normal target window to merge into");
  }
  if (!Array.isArray(windowIds)) {
    windowIds = mergeable.map((w) => w.id).filter((id) => id !== targetWindowId);
  } else {
    // The mergeable filter applies to the SERVICE-SUPPLIED list too, not only to the
    // manual `{}` branch (§9 "Окна типа popup/devtools/app не сливаются"). Same reason
    // as the target validation above: the named window may be a popup or a fullscreen
    // showcase by the time the command lands.
    windowIds = windowIds.filter((id) => mergeableIds.has(id));
  }

  // §9 edge re-check (parity with close_tab's `expect`): the merge was decided on the
  // step-3 snapshot, so re-verify the VOLATILE guards against LIVE state before moving.
  // A SOURCE window the owner returned to in the sub-second gap before step 9 — it has
  // an audible tab, or its active tab is the one on screen — is dropped here and
  // re-decided next pass ("пока с окном работают, оно не трогается", §9). The target is
  // never dropped: idle sources fold INTO the window in use.
  //
  // fullscreen is NOT re-checked here: it is a STRUCTURAL disqualification handled by
  // `isMergeableWindow` above, which — unlike this filter — also bars it from being the
  // TARGET. Keeping it only here was the bug: the line below deliberately exempts the
  // target, so a fullscreen showcase passed straight through as the destination.
  // `tabs` / `onScreenId` are the live reads taken at the top of this command.
  const inUse = new Set();
  for (const t of tabs) {
    if (t.audible || (t.active && t.windowId === onScreenId)) inUse.add(t.windowId);
  }
  windowIds = windowIds.filter((id) => id === targetWindowId || !inUse.has(id));

  // The windows whose activity we must not count while Chrome reshuffles them.
  const marked = [...new Set([...windowIds, targetWindowId])].filter(
    (id) => id !== undefined && id !== null,
  );
  await map.markCuratorCause(marked, nowFn());

  // §9: NEVER move a pinned tab across windows — a cross-window tabs.move silently
  // resets `pinned` (undocumented Chromium; intra-window move keeps it), and a lost
  // turn between the move and re-pinning would destroy the owner's only "do not
  // touch by hand" shield. Only unpinned tabs migrate; a source window left with
  // pinned tabs simply does not disappear.
  const toMove = tabs
    .filter((t) => windowIds.includes(t.windowId) && t.windowId !== targetWindowId && !t.pinned)
    .map((t) => t.id);

  try {
    if (toMove.length > 0) {
      await chrome.tabs.move(toMove, { windowId: targetWindowId, index: -1 });
    }
  } catch (e) {
    await map.clearCuratorCause(marked);
    const msg = String((e && e.message) || e);
    // Chrome refuses tab edits mid-drag ("Tabs cannot be edited right now (user
    // may be dragging a tab)"): that is a transient busy, not a hard failure.
    if (/drag/i.test(msg)) {
      return fail(ERR_BUSY_DRAGGING, msg);
    }
    return fail(ERR_INTERNAL, msg);
  }
  return ok({ merged: toMove.length });
}

// move_tab {tabId, windowId, index?}. Move ONE tab to a window and position inside
// THIS browser. Cross-INSTANCE relocation is the open+close pair of §7 and works
// only because the browsers are separate processes; between the windows of one
// browser there was no verb at all, though `chrome.tabs.move` has driven
// merge_windows all along. `index` defaults to -1, chrome's own "append to the end".
//
// §9's PINNED rule applies verbatim and is the reason this refusal has a code of its
// own. A cross-window `tabs.move` silently resets `pinned` (undocumented Chromium;
// an intra-window move keeps it), and a lost turn between the move and re-pinning
// destroys the owner's only "do not touch by hand" shield. merge_windows answers
// that by SKIPPING pinned tabs — it moves a set, and the skip is visible in the
// `merged` count it returns. A one-tab verb has no such room: skipping silently and
// answering ok would tell the agent the tab moved when it did not. So the whole
// command refuses with `pinned_cross_window` and moves nothing, which the agent can
// tell apart from a generic failure and act on (unpin by hand, or reorder the tab
// inside its own window instead).
//
// INSIDE one window a pinned tab moves freely: `pinned` survives the move, so there
// is no shield to lose and nothing to protect against.
// #45 move_tab {windowId:null}: extract ONE tab into a brand-new BACKGROUND normal
// window. `chrome.windows.create({tabId})` moves the existing tab in — no open+close,
// no new tab id — and goes down the SAME Chromium path a cross-window `tabs.move` takes,
// so it strips `pinned`. Two consequences handled here:
//
//   1. A PINNED tab is refused with `pinned_cross_window` (§9), the identical shield
//      the in-window cross-move honours: losing `pinned` across the move would destroy
//      the owner's only "do not touch by hand" signal.
//   2. The clock is preserved BY HAND. moveTab normally marks BOTH windows UP FRONT so
//      the target activation is not read as "a human looked at the tab", but the new
//      window's id does not exist until windows.create. So we mark the SOURCE before the
//      move (a neighbour activates there) and READ the tab's activity-map age; then AFTER
//      create — once its id exists — we mark the NEW window too AND re-seed the age. The
//      mark suppresses a late onActivated; the seed corrects one delivered during create.
//      Without both, the onActivated Chrome fires in the new window would rejuvenate the
//      tab and it would read as fresh for steps 4-8.
async function extractTabToNewWindow(tab, nowFn, map) {
  if (tab.pinned) {
    return fail(
      ERR_PINNED_CROSS_WINDOW,
      "a pinned tab is never moved across windows (§9) — unpin it, or move it inside its own window",
    );
  }
  // Mark the SOURCE before the move (its neighbour activation must not count as
  // activity). The new window is unmarkable — its id is unknown until windows.create,
  // and curatorCause is keyed by windowId — so its rejuvenation is undone by the restore
  // below rather than suppressed up front.
  await map.markCuratorCause(tab.windowId, nowFn());
  // Read the age the tab had BEFORE the move so it can be restored verbatim afterwards.
  const before = await map.readMap();
  const rec = before && before.tabs ? before.tabs[tab.id] : undefined;
  let win;
  try {
    win = await chrome.windows.create({ tabId: tab.id, focused: false, state: "normal" });
  } catch (e) {
    // The move failed => undo the source mark so a later REAL activation still counts.
    await map.clearCuratorCause(tab.windowId);
    const msg = String((e && e.message) || e);
    if (/drag/i.test(msg)) {
      return fail(ERR_BUSY_DRAGGING, msg);
    }
    // The tab was closed between the `get` above and windows.create; Chromium answers
    // "No tab with id: N". The vanished tab, not an internal fault (same as tabs.move).
    if (/no tab with id|no such tab/i.test(msg)) {
      return fail(ERR_NO_SUCH_TAB, msg);
    }
    return fail(ERR_INTERNAL, msg);
  }
  const created = win && win.tabs && win.tabs[0];
  const newWindowId = win && win.id;
  // Mark the NEW window curator-caused now that its id exists: Chrome fires an
  // onActivated in it (the moved tab becomes active in the fresh window) which must NOT
  // be read as a human touch. This closes the race the normal move avoids by marking
  // both windows UP FRONT: an onActivated delivered AFTER this mark is suppressed by it;
  // one Chrome already delivered BEFORE it (during windows.create) is corrected by the
  // seed below. Together they preserve the clock in every enqueue order.
  await map.markCuratorCause(newWindowId, nowFn());
  // Restore the pre-move clock AND churn (§5): re-seed lastActive/openedAt from the
  // record read above so the extracted tab stays exactly as old as it was, and CARRY its
  // docChanges/lastDocKey/selfNavigating — the tab keeps its id across windows.create, so
  // losing its self-navigation state would let its next doc change re-juvenate it (the
  // next pass must not see it as freshly touched). A tab with no prior record has nothing
  // to preserve.
  if (rec) {
    const nowMs = nowFn();
    await map.seedCuratorTab(
      tab.id,
      {
        seed_age_ms: nowMs - rec.lastActive,
        seed_opened_ago_ms: nowMs - rec.openedAt,
        seed_age_unknown: !!rec.ageUnknown,
        carry_doc_changes: rec.docChanges,
        carry_last_doc_key: rec.lastDocKey,
        carry_self_navigating: rec.selfNavigating,
      },
      nowMs,
    );
  }
  // Same shape as a normal move; `windowId` is the CREATED window and its tab is the
  // sole one, so index 0 (chrome reports it on the created window's tab).
  const resultIndex = created && created.index !== undefined ? created.index : 0;
  return ok({ tabId: tab.id, windowId: newWindowId, index: resultIndex });
}

async function moveTab(params, nowFn, map) {
  // #49 bulk: an `items` array is the LIST form — loop item by item, ONE frame back.
  if (Array.isArray(params.items)) {
    return await moveTabBulk(params, nowFn, map);
  }
  const tabId = params.tabId;
  const targetWindowId = params.windowId;
  // #45: `windowId: null` is the "extract into a NEW window" address — the one legal
  // non-integer. Any OTHER non-integer (a string, undefined, a float) is still a
  // malformed frame refused at the edge.
  const extractToNew = targetWindowId === null;
  if (!extractToNew && !Number.isInteger(targetWindowId)) {
    return fail(ERR_PRECONDITION_FAILED, "move_tab requires an integer windowId or null");
  }
  // The position is optional; -1 is chrome.tabs.move's own "append to the end".
  // Anything below that is rejected HERE rather than left to throw as `internal`.
  // (The new-window path has no position to give — its tab is the window's only one.)
  const index = params.index === undefined || params.index === null ? -1 : params.index;
  if (!Number.isInteger(index) || index < -1) {
    return fail(ERR_PRECONDITION_FAILED, "move_tab index must be an integer >= -1");
  }

  // Never assume the tab is still there: the agent decided on a mirror that is
  // minutes old and the human may have closed the tab since.
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${tabId}`);
  }

  if (extractToNew) {
    return await extractTabToNewWindow(tab, nowFn, map);
  }

  // The SAME eligibility predicate merge_windows applies to its target (§9): a
  // popup / devtools / app window and a fullscreen showcase are not places to drop a
  // tab into. Read LIVE, for merge_windows' reason — by command time the named
  // window may be closed or have become a popup. `no_window` is in the service's
  // _CLIENT_ERRORS set (src/api/instances.py), i.e. "your picture is stale, refetch".
  const windows = await chrome.windows.getAll();
  const target = windows.find((w) => w.id === targetWindowId);
  if (!isMergeableWindow(target)) {
    return fail(ERR_NO_WINDOW, `no eligible target window: ${targetWindowId}`);
  }

  const crossWindow = tab.windowId !== targetWindowId;
  if (crossWindow && tab.pinned) {
    return fail(
      ERR_PINNED_CROSS_WINDOW,
      "a pinned tab is never moved across windows (§9) — unpin it, or move it inside its own window",
    );
  }

  // Both windows get reshuffled by the move (the source activates a neighbour, the
  // target re-activates), so BOTH are marked before it and the mark is AWAITED —
  // exactly as close_tab and merge_windows do. Without it the agent's move reads as
  // "the human touched this tab" and resets the idle clock it was moved by.
  const marked = [...new Set([tab.windowId, targetWindowId])].filter(
    (id) => id !== undefined && id !== null,
  );
  await map.markCuratorCause(marked, nowFn());
  try {
    await chrome.tabs.move(tabId, { windowId: targetWindowId, index });
  } catch (e) {
    // The move failed => undo the mark so a later REAL activation still counts.
    await map.clearCuratorCause(marked);
    const msg = String((e && e.message) || e);
    // Chromium refuses tab edits mid-drag — transient busy, not a failure (§9).
    if (/drag/i.test(msg)) {
      return fail(ERR_BUSY_DRAGGING, msg);
    }
    // The tab was closed in the gap between the `get` above and the move; Chromium
    // answers "No tab with id: N". That is the vanished tab, not an internal fault,
    // and the caller reads the same code it would have got a millisecond earlier.
    if (/no tab with id|no such tab/i.test(msg)) {
      return fail(ERR_NO_SUCH_TAB, msg);
    }
    return fail(ERR_INTERNAL, msg);
  }
  return ok({ tabId, windowId: targetWindowId, index });
}

// --- #49 bulk verbs (ONE frame per list, looped item by item) ----------------
//
// The owner chose "one command for the whole list": the socket carries ONE `command`
// frame whose params hold an `items` array, the extension LOOPS it in its own
// try/catch, and answers ONE `response` carrying `{results:[{index, ok, ...}]}`. The
// match key is `index` (position in the input) — an open item has no id before it
// opens. The native array forms (chrome.tabs.move/remove accept arrays) are NOT used:
// they are fail-fast and report nothing per element.
//
// PERF (§6 command budget): everything that is not per-item is hoisted OUT of the loop —
// ONE chrome.tabs.query({}), ONE chrome.windows.getLastFocused(), ONE activity-map read
// (only when an item actually needs it), and ONE markCuratorCause([...affected windows])
// BEFORE the loop. Else a 20-item list would do 20× runExclusive (loadMap+saveMap of the
// WHOLE map) and a 200-tab map can blow the cmd_timeout_ms budget.

// close_tab {items:[{tabId, expect?}]}. Loop, per-item guard re-check + remove, ONE
// frame back. The per-item `expect` is the SAME guard set as the single form (§6):
// today the pure bulk close sends none (behaves exactly like single close); only the
// bulk RELOCATE close carries url/notAudible/notPinned per item. The volatile guards
// are re-checked live against the ONE hoisted tab query / focus read / map read.
async function closeTabBulk(params, nowFn, map) {
  const items = params.items;
  // ONE tab query (id -> live tab), ONE focused-window read — hoisted out of the loop.
  const allTabs = await chrome.tabs.query({});
  const tabById = new Map(allTabs.map((t) => [t.id, t]));
  const focused = await chrome.windows.getLastFocused();
  // ONE map read, and only when an item actually needs the idle guard (none does today).
  const needMap = items.some(
    (it) => it && it.expect && typeof it.expect.minIdleMs === "number" && it.expect.minIdleMs > 0,
  );
  const mapSnapshot = needMap ? await map.readMap() : null;

  // Mark EVERY affected window ONCE before the loop (a close activates a neighbour in
  // each). Affected = windows of the items whose tab actually exists.
  const affected = [
    ...new Set(
      items
        .map((it) => tabById.get(it && it.tabId))
        .filter((t) => t && t.windowId !== undefined && t.windowId !== null)
        .map((t) => t.windowId),
    ),
  ];
  await map.markCuratorCause(affected, nowFn());

  const results = [];
  let removed = 0;
  const successWindows = new Set(); // windows that saw >=1 real close (a neighbour activated)
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index] || {};
    const tabId = item.tabId;
    const expect = item.expect || {};
    const tab = tabById.get(tabId);
    if (!tab) {
      results.push({ index, ok: false, tabId, error: ERR_NO_SUCH_TAB });
      continue;
    }
    // The SAME volatile guards the single close re-checks (§6) — live, per item.
    if (expect.url !== undefined && tab.url !== expect.url) {
      results.push({ index, ok: false, tabId, error: ERR_PRECONDITION_FAILED, message: "url diverged" });
      continue;
    }
    if (expect.notAudible && tab.audible) {
      results.push({ index, ok: false, tabId, error: ERR_PRECONDITION_FAILED, message: "became audible" });
      continue;
    }
    if (expect.notPinned && tab.pinned) {
      results.push({ index, ok: false, tabId, error: ERR_PRECONDITION_FAILED, message: "was pinned" });
      continue;
    }
    if (tab.active && focused && focused.focused && focused.id === tab.windowId) {
      results.push({ index, ok: false, tabId, error: ERR_PRECONDITION_FAILED, message: "active in focused window" });
      continue;
    }
    if (typeof expect.minIdleMs === "number" && expect.minIdleMs > 0) {
      const rec = mapSnapshot && mapSnapshot.tabs ? mapSnapshot.tabs[tabId] : undefined;
      const idleMs = rec ? nowFn() - rec.lastActive : -1;
      if (idleMs < expect.minIdleMs) {
        results.push({ index, ok: false, tabId, error: ERR_PRECONDITION_FAILED, message: "idle below minIdleMs" });
        continue;
      }
    }
    try {
      await chrome.tabs.remove(tabId);
      removed += 1;
      successWindows.add(tab.windowId);
      results.push({ index, ok: true, tabId });
    } catch (e) {
      results.push({ index, ok: false, tabId, error: ERR_INTERNAL, message: String((e && e.message) || e) });
    }
  }
  // Clear the mark of every affected window that saw NO successful close: no neighbour was
  // activated there, so keeping the mark would spuriously suppress a real user onActivated
  // for CURATOR_CAUSE_WINDOW_MS — worst in the focused window, whose active tab a guard just
  // refused. A window with >=1 close keeps its mark (a neighbour DID activate). This mirrors
  // the single close, which marks a window only around a real remove.
  const unusedWindows = affected.filter((w) => !successWindows.has(w));
  if (unusedWindows.length > 0) {
    await map.clearCuratorCause(unusedWindows);
  }
  return ok({ results });
}

// move_tab {items:[{tabId}], windowId, index?}. ONE shared target window + position for
// the whole list (windowId:null extract-to-new is refused at the MCP door — "one new
// window for all" is a different, unrequested op — so it never reaches here as a list).
async function moveTabBulk(params, nowFn, map) {
  const items = params.items;
  const targetWindowId = params.windowId;
  const index = params.index === undefined || params.index === null ? -1 : params.index;

  // Shared validation, hoisted: a bad index or an ineligible/vanished target fails EVERY
  // item identically and sends nothing to the browser.
  if (!Number.isInteger(index) || index < -1) {
    return ok({
      results: items.map((it, i) => ({
        index: i, ok: false, tabId: it && it.tabId,
        error: ERR_PRECONDITION_FAILED, message: "index must be an integer >= -1",
      })),
    });
  }
  const windows = await chrome.windows.getAll();
  const target = windows.find((w) => w.id === targetWindowId);
  const allTabs = await chrome.tabs.query({});
  const tabById = new Map(allTabs.map((t) => [t.id, t]));
  if (!isMergeableWindow(target)) {
    return ok({
      results: items.map((it, i) => ({
        index: i, ok: false, tabId: it && it.tabId,
        error: ERR_NO_WINDOW, message: `no eligible target window: ${targetWindowId}`,
      })),
    });
  }

  // Mark the target and every source window ONCE (a move re-activates in the target and
  // activates a neighbour in each emptied source).
  const affected = [
    ...new Set(
      [targetWindowId].concat(
        items
          .map((it) => tabById.get(it && it.tabId))
          .filter((t) => t && t.windowId !== undefined && t.windowId !== null)
          .map((t) => t.windowId),
      ),
    ),
  ];
  await map.markCuratorCause(affected, nowFn());

  const results = [];
  let moved = 0;
  const successWindows = new Set(); // windows a real move touched (source neighbour + target)
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i] || {};
    const tabId = item.tabId;
    const tab = tabById.get(tabId);
    if (!tab) {
      results.push({ index: i, ok: false, tabId, error: ERR_NO_SUCH_TAB });
      continue;
    }
    // §9's pinned shield: a cross-window move silently strips `pinned`, so a pinned tab
    // is refused and stays put — exactly the single move's behaviour, per item.
    if (tab.windowId !== targetWindowId && tab.pinned) {
      results.push({ index: i, ok: false, tabId, error: ERR_PINNED_CROSS_WINDOW, message: "pinned cross-window" });
      continue;
    }
    try {
      await chrome.tabs.move(tabId, { windowId: targetWindowId, index });
      moved += 1;
      successWindows.add(tab.windowId);   // source: a neighbour activated as the tab left
      successWindows.add(targetWindowId); // target: the tab arrived (re-activated there)
      results.push({ index: i, ok: true, tabId, windowId: targetWindowId });
    } catch (e) {
      const msg = String((e && e.message) || e);
      if (/drag/i.test(msg)) {
        results.push({ index: i, ok: false, tabId, error: ERR_BUSY_DRAGGING, message: msg });
      } else if (/no tab with id|no such tab/i.test(msg)) {
        results.push({ index: i, ok: false, tabId, error: ERR_NO_SUCH_TAB, message: msg });
      } else {
        results.push({ index: i, ok: false, tabId, error: ERR_INTERNAL, message: msg });
      }
    }
  }
  // Clear the mark of every affected window that saw NO successful move (same §5 reason as
  // the bulk close): a window whose items were all refused had no neighbour activated, so
  // its mark would spuriously suppress a real onActivated. A window touched by >=1 move
  // (as source or target) keeps its mark.
  const unusedWindows = affected.filter((w) => !successWindows.has(w));
  if (unusedWindows.length > 0) {
    await map.clearCuratorCause(unusedWindows);
  }
  return ok({ results });
}

// open_tab {items:[{url, pinned, active, seed_age_ms, seed_opened_ago_ms, seed_age_unknown}]}.
// The §9 target window is picked ONCE (hoisted) and every copy lands there; each item is
// still opened + seeded in its own try/catch so one bad url never aborts the list. This
// list form (not the MCP open_tab verb) is what the bulk relocate opens its copies with.
async function openTabBulk(params, nowFn, map) {
  const items = params.items;
  // ONE window pick for the whole list (null => zero normal windows: each item then
  // creates its own background window, exactly as the single path does).
  const windowId = await pickNormalWindowId();

  const results = [];
  // Seeds are collected and written ONCE after the loop (§5 clock inheritance): the
  // whole point of one frame per list is not paying per-element costs, and a per-item
  // seed is a full map load+save each. Deferring the write does not race onCreated any
  // worse than the single path already does — both land on the same mutation chain,
  // and the seed is what wins either way.
  const seeds = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index] || {};
    if (!isHttpUrl(item.url)) {
      results.push({ index, ok: false, error: ERR_PRECONDITION_FAILED, message: "only http/https urls" });
      continue;
    }
    let tab;
    try {
      tab = await createCuratorTab(item, windowId);
    } catch (e) {
      const msg = String((e && e.message) || e);
      if (/drag/i.test(msg)) {
        results.push({ index, ok: false, error: ERR_BUSY_DRAGGING, message: msg });
        continue;
      }
      if (windowId !== null && /no window/i.test(msg)) {
        // The chosen window vanished mid-list — retry this item in a fresh window (the
        // auto-select fallback the single path uses; never for a caller-named window).
        try {
          tab = await createCuratorTab(item, null);
        } catch (e2) {
          results.push({ index, ok: false, error: ERR_INTERNAL, message: String((e2 && e2.message) || e2) });
          continue;
        }
      } else {
        results.push({ index, ok: false, error: ERR_INTERNAL, message: msg });
        continue;
      }
    }
    if (!tab || tab.id === undefined) {
      results.push({ index, ok: false, error: ERR_NO_WINDOW, message: "could not create tab" });
      continue;
    }
    seeds.push({ tabId: tab.id, seed: item });
    results.push({ index, ok: true, tabId: tab.id, windowId: tab.windowId });
  }
  if (seeds.length) await map.seedCuratorTabs(seeds, nowFn());
  return ok({ results });
}

// execute_js {code, tabId?, world?, awaitPromise?}. Gated on the options checkbox in
// chrome.storage.local, read FRESH here (default OFF). Off => js_disabled and NOT
// executed. On => run the code in the requested world via chrome.scripting.
//
// `awaitPromise` (default false) makes the source the body of an async function so
// top-level `await`/`return` work and chrome awaits the returned promise — see
// evalInWorld. Omitted, the eval path is unchanged.
async function executeJs(params) {
  const stored = await chrome.storage.local.get(ALLOW_EXECUTE_JS_KEY);
  const allowed = !!(stored && stored[ALLOW_EXECUTE_JS_KEY]);
  if (!allowed) {
    return fail(ERR_JS_DISABLED, "execute_js is disabled in this copy's options");
  }
  // Edge-guard the TARGET tab's scheme, exactly like open_tab/navigate_tab (§12):
  // with <all_urls> granted, a raw target could inject into a file:///view-source:
  // page (a MAIN-world eval on file:// reads local files same-origin). Make the
  // http/https invariant independent of host_permissions, not a side effect of it.
  let tab;
  try {
    tab = await chrome.tabs.get(params.tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  if (!isHttpUrl(tab.url)) {
    return fail(ERR_PRECONDITION_FAILED, "execute_js target is not an http/https tab");
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId: params.tabId },
    world: params.world || "MAIN",
    func: evalInWorld,
    args: [String(params.code == null ? "" : params.code), !!params.awaitPromise],
  });
  return ok({ results });
}

// --- chrome.debugger foundation + set_focus_emulation (§12, wave 18) ----------
//
// The tabs THIS extension currently holds a debugger attached to. Module-level, and
// deliberately in-memory: the emulation set by `Emulation.setFocusEmulationEnabled` holds
// ONLY while the debugger stays attached, so an enabled tab must stay attached, and this set
// is how a second enable knows not to attach twice and how a disable knows there is
// something to detach.
//
// ⚠️ MV3 LIFETIME: the service worker can die and be resurrected, losing this in-memory set
// (and, with it, chrome.debugger drops every attachment the dead worker held — detach is
// implicit on worker teardown). For THIS slice that is acceptable: a lost attachment means
// the emulation lapses and a fresh enable re-attaches cleanly. A later slice moves the set
// into chrome.storage.session so a resurrected worker can reconcile.
const debuggerAttachedTabs = new Set();

// The chrome.debugger protocol version to attach with (CDP 1.3).
const DEBUGGER_PROTOCOL_VERSION = "1.3";

// chrome.debugger.onDetach cleanup (§12). Registered ONCE at SW init (service-worker.js).
// The debugger detaches on its own for reasons outside this verb — the human closed the tab
// (`target_closed`) or opened DevTools on it (`canceled_by_user`) — and if the tab is not
// dropped from our set here, a later enable would skip the attach (thinking it is still
// attached) and the sendCommand would throw, or a disable would try to detach a tab the
// browser already released. Keep the set honest by mirroring every detach.
export function handleDebuggerDetach(source) {
  if (source && typeof source.tabId === "number") {
    debuggerAttachedTabs.delete(source.tabId);
  }
}

// Test-only: reset the module-level attachment set between cases (the set is process-global,
// so a leftover entry from one test would leak into the next).
export function __resetDebuggerState() {
  debuggerAttachedTabs.clear();
}

// set_focus_emulation {tabId, enabled} (§12, wave 18). Makes a BACKGROUND tab behave as
// focused (no timer throttling) without taking the screen from the human. STATEFUL: the
// emulation holds only while the debugger is attached.
//
//   enabled=true  — attach the debugger (unless already ours) and turn emulation on, then
//                   KEEP it attached. Idempotent: a repeat enable on an already-attached tab
//                   just re-sends the command, no second attach.
//   enabled=false — turn emulation off (best-effort) and detach, dropping the tab from our
//                   set. A tab we do not hold is an idempotent no-op success.
//
// Gated at the edge by the SINGLE JS & Debugger checkbox (ALLOW_EXECUTE_JS_KEY), exactly
// like execute_js — but it carries NO arbitrary code and writes NO js_audit row.
async function setFocusEmulation(params) {
  const stored = await chrome.storage.local.get(ALLOW_EXECUTE_JS_KEY);
  const allowed = !!(stored && stored[ALLOW_EXECUTE_JS_KEY]);
  if (!allowed) {
    return fail(ERR_JS_DISABLED, "JS & Debugger is disabled in this copy's options");
  }
  const tabId = params.tabId;
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${tabId}`);
  }
  // Edge-guard the target scheme like every other debugger/scripting verb (§12): the
  // debugger must never attach to a chrome://, file:// or other privileged surface.
  if (!isHttpUrl(tab.url)) {
    return fail(ERR_PRECONDITION_FAILED, "set_focus_emulation target is not an http/https tab");
  }

  const enabled = !!params.enabled;

  if (enabled) {
    // Track whether THIS call is the one that attached the tab, so the sendCommand catch
    // below only rolls back an attach we ourselves just made (see there).
    let attachedNow = false;
    // Attach only if this tab is not already ours — a tab takes ONE debugger client, so a
    // second attach on our own tab would throw. A repeat enable is therefore just a
    // re-issued command.
    //
    // Accepted race: frames are dispatched concurrently (`_onMessage` in connection.js does
    // not await), so two simultaneous enable calls on the SAME tab can both read has()===false
    // before either add()s. The second attach then throws, and the losing call returns
    // debugger_attach even though the first call attached successfully. The end state is still
    // consistent (the tab is in the Set once, attached once), so this is a deliberately
    // acceptable race. We do NOT "fix" it with an optimistic add() before attach: that would
    // let the losing call's rollback delete the winner's record — strictly worse.
    if (!debuggerAttachedTabs.has(tabId)) {
      try {
        await chrome.debugger.attach({ tabId }, DEBUGGER_PROTOCOL_VERSION);
      } catch {
        return fail(
          ERR_DEBUGGER_ATTACH,
          "could not attach debugger — DevTools open on this tab, or another client attached",
        );
      }
      debuggerAttachedTabs.add(tabId);
      attachedNow = true;
    }
    try {
      await chrome.debugger.sendCommand({ tabId }, "Emulation.setFocusEmulationEnabled", {
        enabled: true,
      });
    } catch (e) {
      // The attach succeeded but enabling emulation failed. If WE attached the tab in this
      // very call, roll that attach back (best-effort detach + untrack) so we do not leave a
      // visible "debugging this tab" session in an indeterminate state with emulation OFF.
      // If the tab was attached by an EARLIER call (attachedNow===false), leave it alone: that
      // prior session may well be fine and the failure could be transient — tearing it down
      // would break the working attachment on the strength of one failed re-issue.
      if (attachedNow) {
        try {
          await chrome.debugger.detach({ tabId });
        } catch {
          // Already gone or never fully attached — nothing to undo on the browser side.
        }
        debuggerAttachedTabs.delete(tabId);
      }
      return fail(
        ERR_DEBUGGER_ATTACH,
        `could not enable focus emulation: ${String((e && e.message) || e)}`,
      );
    }
    return ok({ enabled: true });
  }

  // enabled=false: a tab we never attached is an idempotent success (nothing to undo).
  if (debuggerAttachedTabs.has(tabId)) {
    // Best-effort: the detach below is what actually drops the emulation (it lapses when the
    // debugger leaves), so a sendCommand that throws — e.g. the tab is mid-teardown — must
    // not stop the detach + untrack.
    try {
      await chrome.debugger.sendCommand({ tabId }, "Emulation.setFocusEmulationEnabled", {
        enabled: false,
      });
    } catch {
      // fall through to detach
    }
    try {
      await chrome.debugger.detach({ tabId });
    } catch {
      // Already gone (tab closed, DevTools took it): the onDetach listener may have cleared
      // it, or will. Either way we drop our record below.
    }
    debuggerAttachedTabs.delete(tabId);
  }
  return ok({ enabled: false });
}
