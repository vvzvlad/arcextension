import { describe, it, expect } from "vitest";

import { createStore } from "../src/lib/store.js";
import { makeChrome, makeFetch } from "./mocks.js";

const NOW = 1_000_000;

function storeWith({ chrome, calls, local }, fetchFn, extra = {}) {
  return createStore({ chromeApi: chrome, fetchFn, now: () => NOW, staleMs: 3000, ...extra });
}

// --- offline-first: fresh profile, NO cache, NO network => still non-empty -----
describe("offline-first first paint (§10)", () => {
  it("renders own tabs from chrome.tabs.query even with no cache and no network", async () => {
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 1, url: "https://own/a", title: "Own A" }],
      messages: { get_identity: { instanceId: "me" } },
      // no `stateCache` in storage.local
    });
    const { fetchFn } = makeFetch({ state: undefined }); // /api/state throws (offline)
    const store = storeWith(env, fetchFn);

    await store.init();
    await store.refresh();

    // NON-EMPTY: own tabs are present from a LOCAL source; the failed refresh did
    // not clear them. (Remove the local-first paint and this reddens.)
    expect(store.ownTabs.value).toHaveLength(1);
    expect(store.ownTabs.value[0].url).toBe("https://own/a");
    expect(store.offline.value).toBe(true);
    expect(store.foreignTabs.value).toEqual([]);
    expect(store.quickLinks.value).toEqual([]);
  });

  it("paints foreign groups + quick links from the storage.local cache (labelled offline)", async () => {
    const cached = {
      state: {
        instances: [{ id: "other", title: "Other", connected: true, snapshot_at: NOW }],
        tabs: [{ instance_id: "other", tab_id: 7, url: "https://foreign/x", title: "FX" }],
        quick_links: [{ id: 1, url: "https://ql/z", title: "Zed", position: 0 }],
      },
      cached_at: NOW - 5000,
    };
    const env = makeChrome({
      tabs: [],
      local: { stateCache: cached },
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({ state: undefined }); // stays offline
    const store = storeWith(env, fetchFn);

    await store.init();
    await store.refresh();

    expect(store.offline.value).toBe(true);
    expect(store.cachedAt.value).toBe(NOW - 5000);
    // Foreign group present from cache, and NOT jumpable while offline (§10).
    const groups = store.foreignGroups.value;
    expect(groups).toHaveLength(1);
    expect(groups[0].instanceId).toBe("other");
    expect(groups[0].jumpable).toBe(false);
    expect(store.filteredQuickLinks.value.map((q) => q.url)).toEqual(["https://ql/z"]);
  });
});

// --- background refresh flips offline off + writes the cache -------------------
describe("background GET /api/state refresh (§10)", () => {
  it("applies live state, marks online, and writes the cache", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const liveState = {
      instances: [{ id: "other", title: "Other", connected: true, snapshot_at: NOW }],
      tabs: [{ instance_id: "other", tab_id: 3, url: "https://foreign/live", title: "L" }],
      quick_links: [],
    };
    const { fetchFn } = makeFetch({ state: { status: 200, body: liveState } });
    const store = storeWith(env, fetchFn);

    await store.init();
    await store.refresh();

    expect(store.offline.value).toBe(false);
    expect(store.foreignTabs.value).toHaveLength(1);
    // The cache was written (so the NEXT fresh-open paints from it).
    expect(env.calls.storageSet.some((o) => o.stateCache)).toBe(true);
  });
});

// --- optimistic quick links (§10) ---------------------------------------------
describe("optimistic quick links (§10)", () => {
  it("an added link appears IMMEDIATELY (before any flush) and is enqueued to the SW", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const { fetchFn } = makeFetch({ state: undefined });
    const store = storeWith(env, fetchFn);

    // No init/refresh: purely local, as if offline.
    store.addQuickLink("https://ql/new", "New QL");

    // Appears immediately (§10 "Постановка в очередь сразу правит кэш"). Remove the
    // optimistic edit in addQuickLink and this reddens.
    expect(store.quickLinks.value.map((q) => q.url)).toContain("https://ql/new");
    const added = store.quickLinks.value.find((q) => q.url === "https://ql/new");
    expect(added.title).toBe("New QL");
    // The SW owns the durable queue — the op was enqueued via runtime.sendMessage.
    expect(env.calls.sendMessage).toContainEqual({
      type: "enqueue_quicklink_op",
      op: "add",
      url: "https://ql/new",
      title: "New QL",
    });
  });

  it("removing a link drops it immediately and enqueues a remove op", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    store.applyState({
      instances: [],
      tabs: [],
      quick_links: [{ id: 5, url: "https://ql/gone", title: "Gone", position: 0 }],
    });

    store.removeQuickLink({ id: 5, url: "https://ql/gone" });

    expect(store.quickLinks.value).toEqual([]);
    expect(env.calls.sendMessage).toContainEqual({
      type: "enqueue_quicklink_op",
      op: "remove",
      id: 5,
    });
  });
});

// --- local search (§10) -------------------------------------------------------
describe("local search (§10)", () => {
  it("filters own tabs, foreign tabs and quick links case-insensitively, grouped", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    store.ownInstanceId.value = "me";
    store.ownTabs.value = [
      { tab_id: 1, window_id: 1, url: "https://own/foreign-lookalike", title: "Own" },
      { tab_id: 2, window_id: 1, url: "https://own/other", title: "Zebra" },
    ];
    store.applyState({
      instances: [{ id: "other", title: "Other", connected: true, snapshot_at: NOW }],
      tabs: [
        { instance_id: "other", tab_id: 9, url: "https://site/x", title: "Foreign One" },
        { instance_id: "other", tab_id: 10, url: "https://site/y", title: "Nope" },
      ],
      quick_links: [
        { id: 1, url: "https://ql/foreign", title: "QL", position: 0 },
        { id: 2, url: "https://ql/z", title: "Zed", position: 1 },
      ],
    });

    store.setSearch("FOREIGN"); // case-insensitive, matches title OR url

    // own: url substring "foreign-lookalike"
    expect(store.filteredOwnTabs.value.map((t) => t.tab_id)).toEqual([1]);
    // foreign: title "Foreign One"
    const groups = store.foreignGroups.value;
    expect(groups).toHaveLength(1);
    expect(groups[0].tabs.map((t) => t.tab_id)).toEqual([9]);
    // quick links: url "https://ql/foreign"
    expect(store.filteredQuickLinks.value.map((q) => q.id)).toEqual([1]);
  });
});

// --- pause (§7) ---------------------------------------------------------------
describe("pause status (§7)", () => {
  it("surfaces paused_until + resume_pending from state (offline-first, via applyState)", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    // applyState is the single writer and runs from the CACHE too — a cached pause
    // renders with no network (§7 "видимость обязательна").
    store.applyState({
      instances: [],
      tabs: [],
      quick_links: [],
      paused_until: 5_000_000,
      resume_pending: true,
    });
    expect(store.pausedUntil.value).toBe(5_000_000);
    expect(store.resumePending.value).toBe(true);

    // A pre-pause state (no key) reads as "not paused", never undefined.
    store.applyState({ instances: [], tabs: [], quick_links: [] });
    expect(store.pausedUntil.value).toBe(null);
    expect(store.resumePending.value).toBe(false);
  });

  it("pauseCurator POSTs /api/pause and reflects the new deadline", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      pausePost: { status: 200, body: { paused_until: 9_000_000, pause_started_at: 1_000_000 } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh(); // online: base/token set

    const res = await store.pauseCurator();
    expect(res.ok).toBe(true);
    expect(counts.pausePost).toBe(1);
    expect(store.pausedUntil.value).toBe(9_000_000);
  });

  it("resumeCurator DELETEs /api/pause, clears the deadline, and re-fetches state", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], paused_until: null } },
      pauseDelete: { status: 200, body: { resumed: true, pass: { status: "no_ready_instances" } } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();
    store.pausedUntil.value = 9_000_000; // pretend a pause was armed

    const before = counts.state;
    const res = await store.resumeCurator();
    expect(res.ok).toBe(true);
    expect(counts.pauseDelete).toBe(1);
    expect(store.pausedUntil.value).toBe(null);
    expect(counts.state).toBe(before + 1); // manual resume re-fetches the truth
  });

  it("offline: pause/resume no-op with an offline note (never throws)", async () => {
    const env = makeChrome({ tabs: [], messages: {} });
    // No init() → base/token stay null → the verbs cannot reach the network.
    const store = storeWith(env, makeFetch({}).fetchFn);
    const p = await store.pauseCurator();
    expect(p.offline).toBe(true);
    expect(store.pauseError.value).toBe("offline");
    const r = await store.resumeCurator();
    expect(r.offline).toBe(true);
  });
});

// --- jump (§10) ---------------------------------------------------------------
describe("jump own (§10)", () => {
  it("activates the tab, focuses its window, and closes the current newtab", async () => {
    let closed = false;
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn, {
      closeSelf: () => {
        closed = true;
      },
    });

    await store.jumpOwn({ tab_id: 5, window_id: 3 });

    expect(env.calls.tabUpdate).toContainEqual([5, { active: true }]);
    expect(env.calls.winUpdate).toContainEqual([3, { focused: true }]);
    expect(closed).toBe(true);
  });
});

describe("jump foreign (§10)", () => {
  it("calls POST /api/focus and, on no_such_tab, RE-FETCHES state (never silent)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const liveState = {
      instances: [{ id: "other", title: "Other", connected: true, snapshot_at: NOW }],
      tabs: [{ instance_id: "other", tab_id: 3, url: "https://foreign/x", title: "X" }],
      quick_links: [],
    };
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: liveState },
      // First focus => no_such_tab (409, refetch); the page must re-fetch /api/state.
      focus: { status: 409, body: { ok: false, error: "no_such_tab", refetch: true } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh(); // online: base/token set, offline=false, state count = 1

    const before = counts.state;
    await store.jumpForeign("other", { tab_id: 3 });

    expect(counts.focus).toBe(1); // POST /api/focus was called
    // no_such_tab => a fresh GET /api/state (re-render), not a silent failure (§10).
    expect(counts.state).toBe(before + 1);
    expect(store.fallbackMessage.value).toBe("");
  });

  it("a successful focus does not re-fetch and shows no fallback", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const liveState = { instances: [], tabs: [], quick_links: [] };
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: liveState },
      focus: { status: 200, body: { ok: true } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    const before = counts.state;
    await store.jumpForeign("other", { tab_id: 3 });

    expect(counts.focus).toBe(1);
    expect(counts.state).toBe(before); // no re-fetch on success
    expect(store.fallbackMessage.value).toBe("");
  });

  it("is inactive offline: no /api/focus, shows the manual fallback", async () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const { fetchFn, counts } = makeFetch({ state: undefined }); // offline
    const store = storeWith(env, fetchFn);
    await store.init(); // offline (no successful refresh)

    await store.jumpForeign("other", { tab_id: 3 });

    expect(counts.focus).toBeUndefined();
    expect(store.fallbackMessage.value).toContain("other");
  });
});
