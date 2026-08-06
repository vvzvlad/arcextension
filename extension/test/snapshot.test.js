import { describe, it, expect, beforeEach } from "vitest";
import { createChromeMock } from "./chrome-mock.js";
import { __resetQueue, onCreated, onDocumentChange, readMap } from "../src/activity-map.js";
import { buildSnapshot } from "../src/snapshot.js";
import { SELF_NAV_LIMIT, IDLE_WINDOW_MS } from "../src/constants.js";

beforeEach(() => {
  __resetQueue();
});

function mockWith(tabs, windows = [], lastFocused = { id: -1, focused: false }) {
  globalThis.chrome = createChromeMock({ tabs, windows, lastFocused });
}

describe("snapshot build — ages not timestamps (§5/§6)", () => {
  // COVERAGE: TabInfo carries ageMs/openedAgoMs and NEVER an absolute client
  // timestamp. Remove the age conversion (emit lastActive directly) and the
  // forbidden-key / forbidden-value assertions redden.
  it("emits ageMs/openedAgoMs and no absolute lastActive/openedAt", async () => {
    mockWith([{ id: 1, windowId: 10, url: "https://a.com/p", title: "A", pinned: false, active: true, audible: false }]);
    await onCreated(1, 1000); // lastActive = openedAt = 1000
    const now = 5000;
    const snap = await buildSnapshot(now, "sess-1");
    const tab = snap.tabs.find((t) => t.tabId === 1);
    expect(tab.ageMs).toBe(now - 1000);
    expect(tab.openedAgoMs).toBe(now - 1000);
    expect(tab).not.toHaveProperty("lastActive");
    expect(tab).not.toHaveProperty("openedAt");
    // No property may equal the absolute stored timestamp.
    expect(Object.values(tab)).not.toContain(1000);
    expect(snap.sessionId).toBe("sess-1");
  });
});

describe("snapshot build — reconcile map vs reality (§5)", () => {
  // COVERAGE: a ghost record (no live tab) is dropped; a live tab with no record
  // is added ageUnknown. Remove the reconciliation and either the ghost persists
  // or the unknown tab is missing/without ageUnknown -> reddens.
  it("drops ghosts and adds unknown tabs with ageUnknown", async () => {
    // Seed a record for tab 1, but make only tab 2 live.
    mockWith([]); // start empty so onCreated has a mock to write into
    await onCreated(1, 1000); // record for a tab that will NOT be live
    // Now the live browser has only tab 2 (unknown to the map).
    chrome.__state.tabs = [{ id: 2, windowId: 20, url: "https://b.com/x", title: "B", active: false }];

    const now = 9000;
    const snap = await buildSnapshot(now, "sess-1");

    const map = await readMap();
    expect(map.tabs[1]).toBeUndefined(); // ghost dropped
    expect(map.tabs[2]).toBeDefined(); // unknown added
    expect(map.tabs[2].ageUnknown).toBe(true);

    expect(snap.tabs.map((t) => t.tabId)).toEqual([2]);
    const t2 = snap.tabs[0];
    expect(t2.ageUnknown).toBe(true);
    expect(t2.ageMs).toBe(0); // freshly added at now
  });
});

describe("snapshot build — selfNavigating clears when marks age out (§5)", () => {
  // COVERAGE (paired with activity-map's threshold test): a page that stopped
  // churning must be able to clear selfNavigating; the ONLY place that can
  // happen with no further event is the docChanges pruning during snapshot
  // build. Remove that pruning/recompute and the flag hangs -> reddens.
  it("selfNavigating set by churn is cleared once marks are older than the idle window", async () => {
    mockWith([{ id: 1, windowId: 10, url: "https://a.com/d/x", title: "A", active: false }]);
    await onCreated(1, 1000);
    for (let i = 0; i <= SELF_NAV_LIMIT; i++) {
      await onDocumentChange(1, `https://a.com/d/${i}`, 1000 + i * 1000);
    }
    let map = await readMap();
    expect(map.tabs[1].selfNavigating).toBe(true);

    // Build a snapshot far enough in the future that EVERY mark is stale (the
    // last mark is at 1000 + SELF_NAV_LIMIT*1000).
    const now = 1000 + SELF_NAV_LIMIT * 1000 + IDLE_WINDOW_MS + 1;
    const snap = await buildSnapshot(now, "sess-1");

    map = await readMap();
    expect(map.tabs[1].docChanges).toEqual([]);
    expect(map.tabs[1].selfNavigating).toBe(false);
    expect(snap.tabs.find((t) => t.tabId === 1).selfNavigating).toBe(false);
  });
});

describe("snapshot build — windows and focus (§6)", () => {
  it("includes windows list and focusedWindowId from live focus", async () => {
    mockWith(
      [{ id: 1, windowId: 10, url: "https://a.com/p", active: true }],
      [{ id: 10, type: "normal", state: "normal" }],
      { id: 10, focused: true },
    );
    await onCreated(1, 1000);
    const snap = await buildSnapshot(5000, "sess-1");
    expect(snap.windows).toEqual([{ id: 10, type: "normal", state: "normal" }]);
    expect(snap.focusedWindowId).toBe(10);
  });

  it("focusedWindowId is null when the browser is unfocused", async () => {
    mockWith(
      [{ id: 1, windowId: 10, url: "https://a.com/p", active: true }],
      [{ id: 10, type: "normal", state: "normal" }],
      { id: 10, focused: false },
    );
    await onCreated(1, 1000);
    const snap = await buildSnapshot(5000, "sess-1");
    expect(snap.focusedWindowId).toBeNull();
  });
});

describe("snapshot build — pendingUrl fallback (§5, freshly created tabs)", () => {
  // COVERAGE: a just-created tab reports an empty `url` with the address in
  // `pendingUrl`. The snapshot must fall back to it, else the tab reaches the
  // mirror address-less. Revert the fallback (url: t.url) and case 1 reddens.
  it("uses pendingUrl when url is empty", async () => {
    mockWith([{ id: 1, windowId: 10, url: "", pendingUrl: "https://x.test/y", title: "X", active: false }]);
    await onCreated(1, 1000);
    const snap = await buildSnapshot(5000, "sess-1");
    const tab = snap.tabs.find((t) => t.tabId === 1);
    expect(tab.url).toBe("https://x.test/y");
  });

  it("falls back to empty string when both url and pendingUrl are absent", async () => {
    mockWith([{ id: 1, windowId: 10, title: "X", active: false }]);
    await onCreated(1, 1000);
    const snap = await buildSnapshot(5000, "sess-1");
    const tab = snap.tabs.find((t) => t.tabId === 1);
    expect(tab.url).toBe("");
  });

  it("prefers a non-empty url over pendingUrl", async () => {
    mockWith([{ id: 1, windowId: 10, url: "https://real.test/a", pendingUrl: "https://pending.test/b", title: "X", active: false }]);
    await onCreated(1, 1000);
    const snap = await buildSnapshot(5000, "sess-1");
    const tab = snap.tabs.find((t) => t.tabId === 1);
    expect(tab.url).toBe("https://real.test/a");
  });
});
