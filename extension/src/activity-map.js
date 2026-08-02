// The activity map — the delicate core of §5.
//
// The map lives in chrome.storage.session (dies with the browser, survives the
// service worker's death — that is exactly why storage.session is chosen). Shape:
//
//   {
//     tabs: { [tabId]: { lastActive, openedAt, ageUnknown, docChanges[], lastDocKey?, selfNavigating? } },
//     windowActiveTab: { [windowId]: tabId },   // "which tab is active in this window"
//     focusedWindowId: number | null,           // cache: ONLY to know the PREVIOUS focused window
//     curatorCause: { [windowId]: timestamp }    // curator wrote-before-operation marks
//   }
//
// ⚠️ ALL mutations go through ONE promise chain (`queue = queue.then(mutate)`).
// Each link does get -> modify -> set and RE-READS the map INSIDE the link, never
// caching it across an await. Storage is async and whole-object last-write-wins:
// two interleaved get/await/set lose an update (MEASURED). The snapshot build is
// ALSO a link of this same chain (see snapshot.js), so it never reads the map in
// the middle of another link's write and reconciles atomically.

import {
  MAP_KEY,
  TICK_MS,
  IDLE_WINDOW_MS,
  SELF_NAV_LIMIT,
  DOC_CHANGES_RING,
  CURATOR_CAUSE_WINDOW_MS,
  WINDOW_ID_NONE,
} from "./constants.js";

// ---------------------------------------------------------------------------
// The single mutation chain.
// ---------------------------------------------------------------------------

let queue = Promise.resolve();

function freshMap() {
  return {
    tabs: {},
    windowActiveTab: {},
    focusedWindowId: null,
    curatorCause: {},
  };
}

async function loadMap() {
  const got = await chrome.storage.session.get(MAP_KEY);
  const map = got && got[MAP_KEY];
  if (!map) return freshMap();
  // Defensive: fill in any absent sibling so links never touch undefined.
  map.tabs ||= {};
  map.windowActiveTab ||= {};
  map.curatorCause ||= {};
  if (map.focusedWindowId === undefined) map.focusedWindowId = null;
  return map;
}

async function saveMap(map) {
  await chrome.storage.session.set({ [MAP_KEY]: map });
}

// Run `fn(map)` as the next link of the single chain. `fn` receives the map read
// INSIDE the link (never cached from before), mutates it in place (and may return
// a value), then the (possibly mutated) map is written back. A rejected link
// never poisons the chain for later links.
export function runExclusive(fn) {
  const result = queue.then(async () => {
    const map = await loadMap();
    const value = await fn(map);
    await saveMap(map);
    return value;
  });
  queue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

// Test seam: reset the chain between tests (the storage mock is recreated per
// test; the resolved queue promise is otherwise harmless but this keeps runs
// independent).
export function __resetQueue() {
  queue = Promise.resolve();
}

// ---------------------------------------------------------------------------
// Small map helpers (operate on an already-loaded map inside a link).
// ---------------------------------------------------------------------------

function newRecord(now, ageUnknown = false) {
  return {
    lastActive: now,
    openedAt: now,
    ageUnknown,
    docChanges: [],
    lastDocKey: undefined,
    selfNavigating: false,
  };
}

// origin + path only — query and fragment are deliberately excluded, because a
// page rewriting only its query/fragment (SPA filters, SSO refresh) is identity
// churn, not activity (§5 "URL как идентичность").
export function documentKey(url) {
  if (typeof url !== "string" || url === "") return "";
  try {
    const u = new URL(url);
    return u.origin + u.pathname;
  } catch {
    return url; // non-URL (chrome://newtab, about:blank …) — compare verbatim
  }
}

function curatorSuppressed(map, windowId, now) {
  const mark = map.curatorCause[windowId];
  return mark !== undefined && now - mark < CURATOR_CAUSE_WINDOW_MS;
}

// ---------------------------------------------------------------------------
// Event handlers (§5 table). Each returns the chain promise so callers can await.
// ---------------------------------------------------------------------------

// onCreated -> new record, lastActive = openedAt = now.
export function onCreated(tabId, now) {
  return runExclusive((map) => {
    map.tabs[tabId] = newRecord(now);
  });
}

// onActivated {tabId, windowId} -> now to this tab AND now to the previous active
// tab of this window; update windowActiveTab. A curator-caused activation within
// CURATOR_CAUSE_WINDOW_MS stamps nothing (only bookkeeps the active tab).
export function onActivated(tabId, windowId, now) {
  return runExclusive((map) => {
    if (curatorSuppressed(map, windowId, now)) {
      // The neighbour Chrome activated after a curator close must NOT reset its
      // clock. Track who is active, but count no activity.
      map.windowActiveTab[windowId] = tabId;
      return;
    }
    const prev = map.windowActiveTab[windowId];
    if (prev !== undefined && prev !== tabId && map.tabs[prev]) {
      map.tabs[prev].lastActive = now; // stamp "left" on the previous active tab
    }
    if (map.tabs[tabId]) {
      map.tabs[tabId].lastActive = now; // onActivated counts even if selfNavigating
    }
    map.windowActiveTab[windowId] = tabId;
  });
}

// onFocusChanged {windowId} -> now to the active tab of the PREVIOUS focused
// window, then overwrite focusedWindowId. The cache exists ONLY to know the
// previous focused window; the tick re-queries focus.
export function onFocusChanged(windowId, now) {
  return runExclusive((map) => {
    const prevFocused = map.focusedWindowId;
    if (
      prevFocused !== null &&
      prevFocused !== undefined &&
      prevFocused !== WINDOW_ID_NONE
    ) {
      const activeTab = map.windowActiveTab[prevFocused];
      if (activeTab !== undefined && map.tabs[activeTab]) {
        map.tabs[activeTab].lastActive = now;
      }
    }
    map.focusedWindowId = windowId;
  });
}

// The tick (alarms, once per TICK_MS): now to the active tab of the focused
// window, ONLY if the browser window is really focused AND the OS is not idle
// (§5). Focus is QUERIED here, not taken from the cache — a stuck cache could
// only cost one extra stamp at a focus change, never a tab that never ages.
export function onTick(now) {
  // The guard queries live focus/idle OUTSIDE the map link; only the stamp is a
  // link. idle argument must be >= 15 s (TICK_MS/1000 = 60).
  return (async () => {
    const win = await chrome.windows.getLastFocused();
    if (!win || win.focused !== true) return false;
    const state = await chrome.idle.queryState(Math.floor(TICK_MS / 1000));
    if (state !== "active") return false;
    return runExclusive((map) => {
      const activeTab = map.windowActiveTab[win.id];
      if (activeTab !== undefined && map.tabs[activeTab]) {
        map.tabs[activeTab].lastActive = now; // tick counts even if selfNavigating
        return true;
      }
      return false;
    });
  })();
}

// onUpdated with a document change (origin+path): now, but at most once per
// TICK_MS; record a mark in the docChanges ring; a tab exceeding SELF_NAV_LIMIT
// doc changes within IDLE_MINUTES becomes selfNavigating (then only onActivated +
// tick count). Query/fragment-only changes are NOT activity.
export function onDocumentChange(tabId, url, now) {
  return runExclusive((map) => {
    const record = map.tabs[tabId];
    if (!record) return; // unknown tab — nothing to stamp
    const key = documentKey(url);
    if (key === record.lastDocKey) return; // only query/fragment changed — not activity
    record.lastDocKey = key;

    // Ring of doc-change marks: drop marks older than the idle window, append
    // now, cap length. Pruning here AND at snapshot build is why selfNavigating
    // can ever clear again once a page stops churning.
    record.docChanges = (record.docChanges || []).filter(
      (ts) => now - ts < IDLE_WINDOW_MS,
    );
    record.docChanges.push(now);
    if (record.docChanges.length > DOC_CHANGES_RING) {
      record.docChanges = record.docChanges.slice(-DOC_CHANGES_RING);
    }
    record.selfNavigating = record.docChanges.length > SELF_NAV_LIMIT;

    // Activity stamp: suppressed for a self-navigating tab, and rate-limited to
    // at most once per TICK_MS so a fast document churn cannot keep a tab young.
    if (!record.selfNavigating && now - record.lastActive >= TICK_MS) {
      record.lastActive = now;
    }
  });
}

// onReplaced {addedTabId, removedTabId} -> MOVE the record and all references to
// the new tabId; the old id is unknown so the NEW record is ageUnknown. Discard
// changes the tabId (MEASURED, §5) — this move is mandatory, not a fresh record.
export function onReplaced(addedTabId, removedTabId, now) {
  return runExclusive((map) => {
    const old = map.tabs[removedTabId];
    if (old) {
      // Carry the age; only the id is unknown, so mark ageUnknown.
      map.tabs[addedTabId] = { ...old, ageUnknown: true };
      delete map.tabs[removedTabId];
    } else {
      // No prior record: the replaced tab is simply unknown -> fresh + ageUnknown.
      map.tabs[addedTabId] = newRecord(now, true);
    }
    // Move windowActiveTab references from the old id to the new id.
    for (const windowId of Object.keys(map.windowActiveTab)) {
      if (map.windowActiveTab[windowId] === removedTabId) {
        map.windowActiveTab[windowId] = addedTabId;
      }
    }
  });
}

// onRemoved -> delete the record (and drop it as any window's active tab).
export function onRemoved(tabId) {
  return runExclusive((map) => {
    delete map.tabs[tabId];
    for (const windowId of Object.keys(map.windowActiveTab)) {
      if (map.windowActiveTab[windowId] === tabId) {
        delete map.windowActiveTab[windowId];
      }
    }
  });
}

// Curator open_tab seed (§5): the record is stamped from the command's seed ages
// so the freshly opened copy inherits the source's age rather than looking new.
// Wired by the command layer in the NEXT phase; the seed path lives here.
export function seedCuratorTab(tabId, seed, now) {
  return runExclusive((map) => {
    const ageMs = Number(seed?.seed_age_ms) || 0;
    const openedAgoMs = Number(seed?.seed_opened_ago_ms) || 0;
    map.tabs[tabId] = {
      lastActive: now - ageMs,
      openedAt: now - openedAgoMs,
      ageUnknown: !!seed?.seed_age_unknown,
      docChanges: [],
      lastDocKey: undefined,
      selfNavigating: false,
    };
  });
}

// curatorCause write-BEFORE-operation (§6). The command layer (next phase) calls
// this BEFORE tabs.remove / tabs.move and AWAITS it; any onActivated in the
// window(s) within CURATOR_CAUSE_WINDOW_MS is then NOT counted as activity. For a
// move, pass BOTH windows (source — where a neighbour is activated — and target).
// Expired marks are cleaned during snapshot build.
export function markCuratorCause(windowIds, now) {
  const ids = Array.isArray(windowIds) ? windowIds : [windowIds];
  return runExclusive((map) => {
    for (const windowId of ids) {
      if (windowId !== undefined && windowId !== null) {
        map.curatorCause[windowId] = now;
      }
    }
  });
}

// Undo a curatorCause mark when the operation failed (§6: "Отметка снимается,
// если операция не удалась").
export function clearCuratorCause(windowIds) {
  const ids = Array.isArray(windowIds) ? windowIds : [windowIds];
  return runExclusive((map) => {
    for (const windowId of ids) {
      delete map.curatorCause[windowId];
    }
  });
}

// Read-only accessor for tests / diagnostics (still a link so it sees a
// consistent map, never a mid-write one).
export function readMap() {
  return runExclusive((map) => JSON.parse(JSON.stringify(map)));
}
