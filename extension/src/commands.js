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
//      edge — otherwise the EXT_TOKEN holder could steer a tab to
//      `data:`/`javascript:` via a rule's canonical_url, bypassing the entire
//      gate built around execute_js.
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
  CMD_NAVIGATE_TAB,
  CMD_MERGE_WINDOWS,
  CMD_EXECUTE_JS,
  ERR_STALE_SESSION,
  ERR_PRECONDITION_FAILED,
  ERR_NO_SUCH_TAB,
  ERR_NO_WINDOW,
  ERR_JS_DISABLED,
  ERR_BUSY_DRAGGING,
  ERR_INTERNAL,
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

// The function body injected into the target world by execute_js. It MUST be a
// top-level, closure-free function: chrome.scripting serializes it to source and
// runs it in the page, so it cannot capture anything from this module.
function evalInWorld(source) {
  // Indirect eval: run the curator-supplied source in the injected world.
  // eslint-disable-next-line no-eval
  return (0, eval)(source);
}

// --- the dispatcher ---------------------------------------------------------

// Execute one command frame. `ctx`:
//   - sessionId: the extension's CURRENT session epoch (§5). A frame whose
//     sessionId differs was minted by a dead session and addresses foreign tabs.
//   - now:  () => ms  (injectable clock; defaults to Date.now)
//   - map:  the activity-map module (injectable for tests)
// Returns `{ok, result}` or `{ok, error:{code, message}}`. Never throws — an
// unexpected failure becomes `{ok:false, error:{code:'internal'}}`.
export async function dispatchCommand(frame, ctx = {}) {
  const nowFn = ctx.now || (() => Date.now());
  const map = ctx.map || activityMap;
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
        return await getTab(params);
      case CMD_FOCUS_TAB:
        return await focusTab(params);
      case CMD_NAVIGATE_TAB:
        return await navigateTab(params);
      case CMD_MERGE_WINDOWS:
        return await mergeWindows(params, nowFn, map);
      case CMD_EXECUTE_JS:
        return await executeJs(params);
      default:
        return fail(ERR_INTERNAL, `unknown command: ${command}`);
    }
  } catch (e) {
    return fail(ERR_INTERNAL, String((e && e.message) || e));
  }
}

// --- verbs ------------------------------------------------------------------

// open_tab {url, pinned, active:false, seed_age_ms, seed_opened_ago_ms,
// seed_age_unknown}. Validate the scheme at the edge, create the tab, then seed
// the activity map so the freshly opened copy inherits the source's age rather
// than reading as brand-new. The seed races onCreated, but the map's single
// mutation chain reconciles them.
async function openTab(params, nowFn, map) {
  if (!isHttpUrl(params.url)) {
    return fail(ERR_PRECONDITION_FAILED, "open_tab accepts only http/https urls");
  }
  const tab = await chrome.tabs.create({
    url: params.url,
    pinned: !!params.pinned,
    active: false, // a curator-opened copy never steals focus
  });
  await map.seedCuratorTab(tab.id, params, nowFn());
  return ok({ tabId: tab.id, windowId: tab.windowId });
}

// close_tab {tabId, expect:{url, notAudible, notPinned, minIdleMs}}. RE-CHECK the
// volatile guards live (not by the stale snapshot). Any divergence => refuse.
// Only when every guard still holds do we mark the curator cause (AWAITED, so the
// mark is durably written before Chrome activates a neighbour) and remove.
async function closeTab(params, nowFn, map) {
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
async function navigateTab(params) {
  if (!isHttpUrl(params.url)) {
    return fail(ERR_PRECONDITION_FAILED, "navigate_tab accepts only http/https urls");
  }
  try {
    await chrome.tabs.update(params.tabId, { url: params.url });
  } catch {
    return fail(ERR_NO_SUCH_TAB, `no such tab: ${params.tabId}`);
  }
  return ok({ ok: true });
}

// merge_windows {windowIds?, targetWindowId?}. Move the source windows' tabs into
// the target window. Mark BOTH the source and target windows BEFORE moving (a
// move activates a neighbour in the emptied source and re-activates in the
// target). Empty params = the manual "merge all" button (§9): every other normal
// window folds into the focused one. A move rejected because the user is dragging
// a tab surfaces as busy_dragging.
async function mergeWindows(params, nowFn, map) {
  let targetWindowId = params.targetWindowId;
  let windowIds = params.windowIds;

  const allWindows = await chrome.windows.getAll();
  // §9 operates on NORMAL windows only: getAll()/getLastFocused() include popup/app
  // windows, and neither their tabs may be folded nor may one become the target.
  const normalWindows = allWindows.filter((w) => w.type === "normal");
  if (targetWindowId === undefined || targetWindowId === null) {
    const focused = await chrome.windows.getLastFocused();
    const focusedNormal =
      focused && focused.type === "normal" && focused.id !== undefined && focused.id !== -1;
    targetWindowId =
      (focusedNormal && focused.id) || (normalWindows[0] && normalWindows[0].id);
  }
  if (targetWindowId === undefined || targetWindowId === null) {
    return fail(ERR_NO_WINDOW, "no normal target window to merge into");
  }
  if (!Array.isArray(windowIds)) {
    windowIds = normalWindows.map((w) => w.id).filter((id) => id !== targetWindowId);
  }

  // §9 edge re-check (parity with close_tab's `expect`): the merge was decided on the
  // step-3 snapshot, so re-verify the VOLATILE guards against LIVE state before moving.
  // A SOURCE window the owner returned to in the sub-second gap before step 9 — it has
  // an audible tab, or its active tab is the one on screen (in the focused window) — is
  // dropped here and re-decided next pass ("пока с окном работают, оно не трогается",
  // §9). The target is never dropped: idle sources fold INTO the window in use.
  const tabs = await chrome.tabs.query({});
  const focusedNow = await chrome.windows.getLastFocused();
  const focusedId =
    focusedNow && focusedNow.type === "normal" && focusedNow.id !== -1 ? focusedNow.id : null;
  const inUse = new Set();
  for (const t of tabs) {
    if (t.audible || (t.active && t.windowId === focusedId)) inUse.add(t.windowId);
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

// execute_js {code, tabId?, world?}. Gated on the options checkbox in
// chrome.storage.local, read FRESH here (default OFF). Off => js_disabled and NOT
// executed. On => run the code in the requested world via chrome.scripting.
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
    args: [String(params.code == null ? "" : params.code)],
  });
  return ok({ results });
}
