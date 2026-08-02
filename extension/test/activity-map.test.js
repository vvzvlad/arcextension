import { describe, it, expect, beforeEach } from "vitest";
import { createChromeMock } from "./chrome-mock.js";
import {
  runExclusive,
  __resetQueue,
  onCreated,
  onActivated,
  onFocusChanged,
  onTick,
  onDocumentChange,
  onReplaced,
  onRemoved,
  markCuratorCause,
  seedCuratorTab,
  readMap,
} from "../src/activity-map.js";
import { TICK_MS, IDLE_WINDOW_MS, SELF_NAV_LIMIT } from "../src/constants.js";

beforeEach(() => {
  globalThis.chrome = createChromeMock();
  __resetQueue();
});

describe("single promise chain (§5)", () => {
  // ACCEPTANCE: parallel map mutations lose no updates. The MEASURED failure is
  // {n:1,b:true} (one update lost) vs the correct {n:2,a:true,b:true}. Because
  // the mock storage is truly async, this reddens if runExclusive stops
  // serializing through the single chain.
  it("two interleaving mutations both land (n:2,a,b) — nothing lost", async () => {
    const inc = (letter) =>
      runExclusive((map) => {
        map.counter = (map.counter || 0) + 1;
        map[letter] = true;
      });
    // Fire both WITHOUT awaiting between them, so they would interleave.
    const pa = inc("a");
    const pb = inc("b");
    await Promise.all([pa, pb]);
    const map = await readMap();
    expect(map.counter).toBe(2);
    expect(map.a).toBe(true);
    expect(map.b).toBe(true);
  });
});

describe("onCreated / onRemoved", () => {
  it("onCreated stamps lastActive = openedAt = now", async () => {
    await onCreated(1, 1000);
    const map = await readMap();
    expect(map.tabs[1]).toMatchObject({ lastActive: 1000, openedAt: 1000, ageUnknown: false });
  });

  it("onRemoved deletes the record and its windowActiveTab reference", async () => {
    await onCreated(1, 1000);
    await onActivated(1, 10, 1000);
    await onRemoved(1);
    const map = await readMap();
    expect(map.tabs[1]).toBeUndefined();
    expect(map.windowActiveTab[10]).toBeUndefined();
  });
});

describe("onActivated (§5)", () => {
  // COVERAGE: onActivated stamps the PREVIOUS active tab of the window too.
  it("stamps both the new tab and the previous active of the window", async () => {
    await onCreated(1, 1000); // prev
    await onActivated(1, 10, 1000); // window 10 active = tab 1
    await onCreated(2, 1000); // cur
    await onActivated(2, 10, 5000);
    const map = await readMap();
    expect(map.tabs[1].lastActive).toBe(5000); // previous active got "left" stamp
    expect(map.tabs[2].lastActive).toBe(5000); // new active got stamp
    expect(map.windowActiveTab[10]).toBe(2);
  });
});

describe("curatorCause suppression (§6)", () => {
  // ACCEPTANCE: a curator close must NOT bump the neighbour Chrome activates. The
  // guard is the CURATOR_CAUSE_WINDOW_MS suppression in onActivated; remove it
  // and the neighbour gets stamped -> this reddens.
  it("onActivated within the window after markCuratorCause does not stamp", async () => {
    await onCreated(7, 1000); // the neighbour, lastActive = 1000
    await markCuratorCause([10], 2000); // curator is about to remove/move in window 10
    await onActivated(7, 10, 2500); // Chrome activates the neighbour 500ms later
    const map = await readMap();
    expect(map.tabs[7].lastActive).toBe(1000); // unchanged — not counted as activity
    expect(map.windowActiveTab[10]).toBe(7); // but bookkeeping is current
  });

  it("onActivated after the window expires DOES stamp (control arm)", async () => {
    await onCreated(7, 1000);
    await markCuratorCause([10], 2000);
    await onActivated(7, 10, 2000 + 2001); // just past CURATOR_CAUSE_WINDOW_MS
    const map = await readMap();
    expect(map.tabs[7].lastActive).toBe(2000 + 2001);
  });
});

describe("onFocusChanged (§5)", () => {
  it("stamps the active tab of the PREVIOUS focused window, then overwrites", async () => {
    await onCreated(1, 1000);
    await onActivated(1, 10, 1000); // window 10 active tab = 1
    await onFocusChanged(10, 1000); // focus is now on window 10
    await onFocusChanged(20, 8000); // focus leaves 10 for 20
    const map = await readMap();
    expect(map.tabs[1].lastActive).toBe(8000); // prev focused window's active tab stamped
    expect(map.focusedWindowId).toBe(20);
  });
});

describe("tick (§5)", () => {
  // COVERAGE: the tick stamps ONLY when the window is really focused AND the OS
  // is not idle. Remove either guard and it stamps regardless -> reddens.
  it("stamps the active tab when focused AND idle=active", async () => {
    await onCreated(1, 1000);
    await onActivated(1, 10, 1000);
    chrome.__state.lastFocused = { id: 10, focused: true };
    chrome.__state.idleState = "active";
    const stamped = await onTick(9000);
    expect(stamped).toBe(true);
    const map = await readMap();
    expect(map.tabs[1].lastActive).toBe(9000);
  });

  it("does NOT stamp when the window is not focused", async () => {
    await onCreated(1, 1000);
    await onActivated(1, 10, 1000);
    chrome.__state.lastFocused = { id: 10, focused: false };
    chrome.__state.idleState = "active";
    const stamped = await onTick(9000);
    expect(stamped).toBe(false);
    const map = await readMap();
    expect(map.tabs[1].lastActive).toBe(1000);
  });

  it("does NOT stamp when the OS is idle", async () => {
    await onCreated(1, 1000);
    await onActivated(1, 10, 1000);
    chrome.__state.lastFocused = { id: 10, focused: true };
    chrome.__state.idleState = "idle";
    const stamped = await onTick(9000);
    expect(stamped).toBe(false);
    const map = await readMap();
    expect(map.tabs[1].lastActive).toBe(1000);
  });
});

describe("onDocumentChange (§5)", () => {
  // COVERAGE: a query/fragment-only change is NOT activity. The guard is the
  // origin+path equality check; time is advanced past the TICK_MS rate limit so
  // ONLY that guard prevents the stamp (non-vacuous: remove it and the second
  // call stamps).
  it("query-only change does not stamp lastActive", async () => {
    await onCreated(1, 1000);
    await onDocumentChange(1, "https://a.com/p?x=1", 1000); // baseline docKey
    let map = await readMap();
    expect(map.tabs[1].lastActive).toBe(1000);
    // Same origin+path, different query, well past the rate-limit window.
    await onDocumentChange(1, "https://a.com/p?x=2", 1000 + TICK_MS + 1);
    map = await readMap();
    expect(map.tabs[1].lastActive).toBe(1000); // unchanged: not a document change
  });

  it("a real document change past the rate limit stamps lastActive", async () => {
    await onCreated(1, 1000);
    await onDocumentChange(1, "https://a.com/p", 1000);
    await onDocumentChange(1, "https://a.com/other", 1000 + TICK_MS + 1);
    const map = await readMap();
    expect(map.tabs[1].lastActive).toBe(1000 + TICK_MS + 1);
  });

  // COVERAGE: selfNavigating sets after > SELF_NAV_LIMIT doc changes, and CLEARS
  // once the marks age out during snapshot build. The threshold guard is here;
  // the clear guard is exercised in snapshot.test.js too.
  it("sets selfNavigating after more than SELF_NAV_LIMIT document changes", async () => {
    await onCreated(1, 1000);
    for (let i = 0; i <= SELF_NAV_LIMIT; i++) {
      // SELF_NAV_LIMIT+1 = 11 distinct documents, all within the idle window.
      await onDocumentChange(1, `https://a.com/d/${i}`, 1000 + i * 1000);
    }
    const map = await readMap();
    expect(map.tabs[1].selfNavigating).toBe(true);
  });

  it("a self-navigating tab is NOT stamped by a further document change", async () => {
    await onCreated(1, 1000);
    for (let i = 0; i <= SELF_NAV_LIMIT; i++) {
      await onDocumentChange(1, `https://a.com/d/${i}`, 1000 + i * 1000);
    }
    let map = await readMap();
    const before = map.tabs[1].lastActive;
    // A later doc change, well past the rate limit: still must not stamp, because
    // the tab is self-navigating (only onActivated + tick count for it).
    await onDocumentChange(1, "https://a.com/d/final", 1000 + IDLE_WINDOW_MS - 1);
    map = await readMap();
    expect(map.tabs[1].selfNavigating).toBe(true);
    expect(map.tabs[1].lastActive).toBe(before);
  });
});

describe("onReplaced — discard changes tabId (§5)", () => {
  // ACCEPTANCE: discard preserves age. The record MOVES to the new id carrying
  // its old lastActive/openedAt (NOT reset to now), and the new id is
  // ageUnknown. Remove the carry (create fresh) and lastActive becomes now ->
  // reddens.
  it("moves the record to the new id, carries age, marks ageUnknown", async () => {
    await onCreated(100, 1000);
    await onActivated(100, 10, 1000); // also a windowActiveTab reference to move
    await onReplaced(200, 100, 9999);
    const map = await readMap();
    expect(map.tabs[100]).toBeUndefined();
    expect(map.tabs[200]).toBeDefined();
    expect(map.tabs[200].lastActive).toBe(1000); // carried, NOT 9999
    expect(map.tabs[200].openedAt).toBe(1000);
    expect(map.tabs[200].ageUnknown).toBe(true);
    expect(map.windowActiveTab[10]).toBe(200); // reference moved
  });
});

describe("curator seed (§5)", () => {
  it("seedCuratorTab stamps from seed ages relative to now", async () => {
    await seedCuratorTab(5, { seed_age_ms: 3000, seed_opened_ago_ms: 8000, seed_age_unknown: true }, 100000);
    const map = await readMap();
    expect(map.tabs[5]).toMatchObject({
      lastActive: 97000,
      openedAt: 92000,
      ageUnknown: true,
    });
  });
});

describe("quota (§5)", () => {
  // ACCEPTANCE: a 500-tab map fits the chrome.storage.session quota. The MEASURED
  // figure is ~288000 B of 10485760; assert comfortably under ~1 MB.
  it("a 500-entry map serializes well under quota", async () => {
    const map = { tabs: {}, windowActiveTab: {}, focusedWindowId: 1, curatorCause: {} };
    for (let i = 0; i < 500; i++) {
      map.tabs[i] = {
        lastActive: 1_700_000_000_000 + i,
        openedAt: 1_699_000_000_000 + i,
        ageUnknown: false,
        docChanges: [1, 2, 3],
        lastDocKey: "https://example.com/some/reasonably/long/path/" + i,
        selfNavigating: false,
      };
      map.windowActiveTab[i % 20] = i;
    }
    const bytes = new TextEncoder().encode(JSON.stringify(map)).length;
    const QUOTA = 10_485_760;
    expect(bytes).toBeLessThan(1_000_000);
    expect(bytes).toBeLessThan(QUOTA);
  });
});
