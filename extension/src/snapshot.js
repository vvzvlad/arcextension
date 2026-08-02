// Snapshot build (§5 "Сборка снимка заодно сверяет карту с реальностью", §6
// "Снимок"). This is a LINK of the same single mutation chain (activity-map's
// runExclusive), so it never reads the map mid-write and its reconciliation of
// the map is atomic with the read that produces the TabInfo list.
//
// Reconciliation while chrome.tabs.query is already on hand:
//   * a record with no live tab  -> deleted (a ghost, else it never expires);
//   * a live tab with no record   -> new record, ageUnknown (else an orphaned tab
//     stays unknown-age forever);
//   * expired curatorCause marks  -> cleaned;
//   * docChanges older than the idle window -> pruned, and selfNavigating
//     recomputed — otherwise a page that stopped churning would have no event
//     left to clear the flag on and it would hang forever.
//
// TabInfo carries ageMs / openedAgoMs, never an absolute client timestamp: no
// absolute client time ever leaves the extension (§5 "Возрасты вместо меток").

import { runExclusive } from "./activity-map.js";
import {
  IDLE_WINDOW_MS,
  SELF_NAV_LIMIT,
  CURATOR_CAUSE_WINDOW_MS,
  WINDOW_ID_NONE,
} from "./constants.js";

// Build a snapshot as a chain link. `now` is Date.now() at build time; the
// caller (the connection) supplies `sessionId` (it owns the epoch), which is
// echoed back into the frame.
export function buildSnapshot(now, sessionId) {
  return runExclusive(async (map) => {
    // chrome.tabs.query is already needed for the snapshot, so the reconciliation
    // rides along with it — all inside this one link (no interleaving mutation).
    const liveTabs = await chrome.tabs.query({});
    const liveWindows = await chrome.windows.getAll();
    const focused = await chrome.windows.getLastFocused();

    const liveIds = new Set(liveTabs.map((t) => t.id));

    // Drop ghosts: records whose tab no longer exists.
    for (const key of Object.keys(map.tabs)) {
      if (!liveIds.has(Number(key))) delete map.tabs[key];
    }
    // Add unknowns: live tabs with no record are fresh + ageUnknown (§5).
    for (const t of liveTabs) {
      if (!map.tabs[t.id]) {
        map.tabs[t.id] = {
          lastActive: now,
          openedAt: now,
          ageUnknown: true,
          docChanges: [],
          lastDocKey: undefined,
          selfNavigating: false,
        };
      }
    }
    // Clean expired curatorCause marks (else the whole cleanup only happens here).
    for (const windowId of Object.keys(map.curatorCause)) {
      if (now - map.curatorCause[windowId] >= CURATOR_CAUSE_WINDOW_MS) {
        delete map.curatorCause[windowId];
      }
    }
    // Prune stale doc-change marks and recompute selfNavigating so a page that
    // stopped churning can clear the flag with no further event.
    for (const key of Object.keys(map.tabs)) {
      const r = map.tabs[key];
      r.docChanges = (r.docChanges || []).filter(
        (ts) => now - ts < IDLE_WINDOW_MS,
      );
      r.selfNavigating = r.docChanges.length > SELF_NAV_LIMIT;
    }

    // Emit TabInfo from the reconciled map. Ages, never timestamps.
    const tabs = liveTabs.map((t) => {
      const r = map.tabs[t.id];
      return {
        tabId: t.id,
        windowId: t.windowId,
        url: t.url,
        title: t.title,
        favIconUrl: t.favIconUrl,
        pinned: !!t.pinned,
        active: !!t.active,
        audible: !!t.audible,
        ageMs: now - r.lastActive,
        openedAgoMs: now - r.openedAt,
        ageUnknown: !!r.ageUnknown,
        selfNavigating: !!r.selfNavigating,
      };
    });

    const windows = liveWindows.map((w) => ({
      id: w.id,
      type: w.type,
      state: w.state,
    }));

    // The window currently on screen. If the browser is unfocused nothing is on
    // screen -> null; the service nulls its guard the same way (§5 "Активность и
    // фокус"). The map.focusedWindowId cache is a separate concern (the tick).
    const focusedWindowId =
      focused && focused.focused && focused.id !== WINDOW_ID_NONE
        ? focused.id
        : null;

    return { sessionId, focusedWindowId, tabs, windows };
  });
}
