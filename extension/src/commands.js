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
  CMD_NAVIGATE_TAB,
  CMD_MERGE_WINDOWS,
  CMD_EXECUTE_JS,
  CMD_MOVE_TAB,
  ERR_STALE_SESSION,
  ERR_PRECONDITION_FAILED,
  ERR_NO_SUCH_TAB,
  ERR_NO_WINDOW,
  ERR_JS_DISABLED,
  ERR_BUSY_DRAGGING,
  ERR_PINNED_CROSS_WINDOW,
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
        return Array.isArray(params.items) ? await getTabBulk(params) : await getTab(params);
      case CMD_FOCUS_TAB:
        return await focusTab(params);
      case CMD_NAVIGATE_TAB:
        return await navigateTab(params);
      case CMD_MERGE_WINDOWS:
        return await mergeWindows(params, nowFn, map);
      case CMD_MOVE_TAB:
        return await moveTab(params, nowFn, map);
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
