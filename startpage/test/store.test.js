import { describe, it, expect } from "vitest";

import { createStore } from "../src/lib/store.js";
import { makeChrome, makeFetch } from "./mocks.js";

const NOW = 1_000_000;

function storeWith({ chrome, calls, local }, fetchFn, extra = {}) {
  return createStore({ chromeApi: chrome, fetchFn, now: () => NOW, ...extra });
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

// --- the queue overlay: a refresh must not erase unflushed ops (§10) ----------
describe("pending quick-link ops survive a refresh (§10)", () => {
  const QUEUE_KEY = "quickLinkQueue";

  it("re-applies the SW queue on top of the server list and CACHES the overlay", async () => {
    // The race: a link is added offline (it lives in the SW's durable queue), a short
    // window of connectivity lets GET /api/state land BEFORE the up-to-60 s tick flush.
    // Applying the server list verbatim drops the link from the UI *and* from the
    // cache; if the network dies again the link is invisible for days while the queue
    // still holds it. Remove overlayPending and this reddens.
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      local: {
        [QUEUE_KEY]: {
          ops: [{ op: "add", url: "https://ql/offline", title: "Offline" }],
          claimed: null,
        },
      },
    });
    const liveState = {
      instances: [],
      tabs: [],
      quick_links: [{ id: 1, url: "https://ql/server", title: "Server", position: 0 }],
      server_now: NOW,
    };
    const { fetchFn } = makeFetch({ state: { status: 200, body: liveState } });
    const store = storeWith(env, fetchFn);

    await store.init();
    await store.refresh();

    expect(store.quickLinks.value.map((q) => q.url)).toEqual([
      "https://ql/server",
      "https://ql/offline",
    ]);
    // The CACHE the next cold open paints from carries the overlay too.
    const cached = env.calls.storageSet.filter((o) => o.stateCache).pop();
    expect(cached.stateCache.state.quick_links.map((q) => q.url)).toContain("https://ql/offline");
  });

  it("applies a CLAIMED (in-flight POST) batch before the still-queued ops", async () => {
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      local: {
        [QUEUE_KEY]: {
          ops: [{ op: "remove", url: "https://ql/server" }],
          claimed: { ops: [{ op: "add", url: "https://ql/claimed" }], key: "k1" },
        },
      },
    });
    const liveState = {
      instances: [],
      tabs: [],
      quick_links: [{ id: 1, url: "https://ql/server", title: "Server", position: 0 }],
      server_now: NOW,
    };
    const { fetchFn } = makeFetch({ state: { status: 200, body: liveState } });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    expect(store.quickLinks.value.map((q) => q.url)).toEqual(["https://ql/claimed"]);
  });

  it("a link added on THIS page survives a refresh that races the SW ack", async () => {
    // The SW ack has not returned yet, so the op is in neither the durable queue nor
    // the server state. Without inFlightOps the refresh would blink it away.
    let releaseAck;
    const env = makeChrome({
      tabs: [],
      messages: {
        get_identity: { instanceId: "me" },
        enqueue_quicklink_op: () => new Promise((r) => (releaseAck = r)),
      },
    });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();

    store.addQuickLink("https://ql/just-added", "Just added");
    await store.refresh(); // lands while the SW ack is still in flight

    expect(store.quickLinks.value.map((q) => q.url)).toEqual(["https://ql/just-added"]);
    releaseAck({ ok: true });
  });

  it("reads the queue BEFORE the request, so a flush landing mid-refresh cannot erase a link", async () => {
    // The window: refresh reads the queue AFTER the response, the SW's tick flush
    // confirms the batch in between, the read comes back empty — and writeCache then
    // overwrites the reconciled cache with a server list fetched BEFORE the flush
    // applied. The link disappears from the UI and from the cache. Reading first can
    // only over-apply, which is idempotent.
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      local: {
        [QUEUE_KEY]: { ops: [{ op: "add", url: "https://ql/offline", title: "Off" }], claimed: null },
      },
    });
    const { fetchFn } = makeFetch({
      state: (n) => {
        // The SW flush drains the queue while THIS response is in flight.
        if (n === 1) env.local[QUEUE_KEY] = { ops: [], claimed: null };
        // ...but the response was built before the server applied it.
        return { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    expect(store.quickLinks.value.map((q) => q.url)).toEqual(["https://ql/offline"]);
    expect(env.local.stateCache.state.quick_links.map((q) => q.url)).toEqual([
      "https://ql/offline",
    ]);
  });

  it("keeps a link enqueued AND acked INSIDE the request window (the mirror hole)", async () => {
    // The symmetric hole of the test above. Reading only BEFORE the request misses an op
    // that was both enqueued and acked while the request was in flight: the ack drained
    // it from inFlightOps, the pre-read never saw it, and the response was built before
    // the server knew about it — so the link disappears from an open page. Only the
    // UNION of the two reads covers both directions.
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      local: { [QUEUE_KEY]: { ops: [], claimed: null } },
    });
    const { fetchFn } = makeFetch({
      state: (n) => {
        // Mid-request: the page enqueues a link and the SW acks it into the durable
        // queue. The response below was computed before any of that.
        if (n === 1) {
          env.local[QUEUE_KEY] = {
            ops: [{ op: "add", url: "https://ql/mid-flight", title: "Mid" }],
            claimed: null,
          };
        }
        return { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    expect(store.quickLinks.value.map((q) => q.url)).toEqual(["https://ql/mid-flight"]);
    expect(env.local.stateCache.state.quick_links.map((q) => q.url)).toEqual([
      "https://ql/mid-flight",
    ]);
  });

  it("an ACKED op stops being overlaid — a stale remove must not eat a re-added link", async () => {
    // Once the SW has the op, it belongs to the durable queue and pendingOps carries
    // it. Failing to drop it from inFlightOps (identity broken by a deep reactive
    // proxy) makes a `remove` immortal: the human re-adds the same url and every
    // refresh deletes it again.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [],
          tabs: [],
          quick_links: [{ id: 1, url: "https://ql/x", title: "X", position: 0 }],
          server_now: NOW,
        },
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();

    store.removeQuickLink({ id: 1, url: "https://ql/x" });
    await Promise.resolve(); // let the SW ack settle (the mock resolves immediately)
    await Promise.resolve();
    await store.refresh(); // the server has NOT applied the remove yet

    // The queue in storage is empty in this mock (no real SW), so with the in-flight
    // op correctly dropped the server list wins and the link is back.
    expect(store.quickLinks.value.map((q) => q.url)).toEqual(["https://ql/x"]);
  });
});

// --- the clock: server scale, and it must TICK (§10) --------------------------
describe("server clock offset (§10)", () => {
  function instanceAt(snapshotAt) {
    return {
      instances: [
        { id: "other", title: "Other", connected: true, last_seen_at: snapshotAt, snapshot_at: snapshotAt },
      ],
      tabs: [],
      quick_links: [],
    };
  }

  it("judges freshness against server_now, not the laptop clock", async () => {
    // The laptop is 10 s BEHIND the server. The mirror was snapshotted at the server's
    // "now", i.e. it is perfectly fresh — but compared to the local clock it looks
    // 10 s old and every instance would read "зеркало устарело" forever (staleMs=3000).
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const serverNow = NOW + 10_000;
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { ...instanceAt(serverNow), server_now: serverNow } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    expect(store.serverOffset.value).toBe(10_000);
    expect(store.statusRows.value[0].status.state).toBe("ok");
  });

  it("a connected instance with an OLD snapshot is still 'на связи' (§62 item 1: no staleness)", async () => {
    // Mirror-freshness is gone (issue #62 item 1): a connected instance reads "на связи"
    // however old its last snapshot is. Reddens if the stale branch is reintroduced.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const serverNow = NOW + 10_000;
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: { ...instanceAt(serverNow - 60_000), server_now: serverNow },
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();
    expect(store.statusRows.value[0].status.state).toBe("ok");
    expect(store.statusRows.value[0].status.label).toBe("на связи");
  });

  it("the 1s tick does NOT rebuild the foreign TAB LISTS (§10 promises hundreds of rows)", async () => {
    // The expensive grouping must keep its identity across a tick so Vue does not re-diff
    // every v-for row. (Status is no longer time-dependent — §62 item 1 — but the tick
    // still invalidates the cheap status layer, so the identity guarantee still matters.)
    let localNow = NOW;
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [
            { id: "other", title: "Other", connected: true, snapshot_at: NOW, last_seen_at: NOW },
          ],
          tabs: [{ instance_id: "other", tab_id: 1, url: "https://f/x", title: "X" }],
          quick_links: [],
          server_now: NOW,
        },
      },
    });
    const store = createStore({
      chromeApi: env.chrome,
      fetchFn,
      now: () => localNow,
    });
    await store.init();
    await store.refresh();

    const listBefore = store.foreignTabGroups.value;
    const tabsBefore = listBefore[0].tabs;
    expect(store.foreignGroups.value[0].status.state).toBe("ok");

    localNow = NOW + 60_000;
    store.tick();

    // A connected instance stays "на связи" regardless of time...
    expect(store.foreignGroups.value[0].status.state).toBe("ok");
    // ...and the expensive grouping was not recomputed at all.
    expect(store.foreignTabGroups.value).toBe(listBefore);
    expect(store.foreignTabGroups.value[0].tabs).toBe(tabsBefore);
  });
});

// --- 423: the stop gate is not a breakage (§7) --------------------------------
describe("stopped verbs offer the human's force override (§7)", () => {
  it("a 423 jump surfaces the stop stamp and retries with force:true on demand", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
      focus: (opts, n) => {
        bodies.push(JSON.parse(opts.body));
        return n === 1
          ? { status: 423, body: { error: "stopped", since: NOW - 3_600_000 } }
          : { status: 200, body: { ok: true } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    await store.jumpForeign("other", { tab_id: 3 });

    // NOT the useless manual fallback: the reason is named and an override offered.
    expect(store.fallbackMessage.value).toBe("");
    expect(store.pauseBlock.value).toMatchObject({ verb: "focus", since: NOW - 3_600_000 });
    expect(bodies[0].force).toBeUndefined(); // never forced automatically

    await store.retryForced();
    expect(bodies[1]).toMatchObject({ instance: "other", tabId: 3, force: true });
    expect(store.pauseBlock.value).toBe(null);
    expect(counts.focus).toBe(2);
  });
});

// --- raise a space (§62 item 3): click an instance row => focus_window ---------
describe("raise a space (§62 item 3)", () => {
  function fleet(extra = {}) {
    return {
      instances: [
        { id: "prox", title: "prox", connected: true, last_seen_at: NOW, snapshot_at: NOW, focused_window_id: 12 },
        { id: "me", title: "me", connected: true, last_seen_at: NOW, snapshot_at: NOW, focused_window_id: 3 },
        { id: "closed", title: "closed", connected: false, last_seen_at: NOW, snapshot_at: NOW, focused_window_id: 5 },
        { id: "nowin", title: "nowin", connected: true, last_seen_at: NOW, snapshot_at: NOW, focused_window_id: null },
      ],
      tabs: [], quick_links: [], server_now: NOW, ...extra,
    };
  }

  it("raises a BACKGROUND instance, whose focused_window_id is null, by its busiest window", async () => {
    // The regression this guards: `focused_window_id` means "the window on screen RIGHT
    // NOW, null when the browser is unfocused" (§5). A foreign browser is unfocused
    // exactly when you want to raise it, so gating on that field alone made the row dead
    // in the only case it exists for. Every other fixture here hard-codes a number,
    // which is a state a background instance is never in.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn, counts } = makeFetch({
      state: {
        status: 200,
        body: fleet({
          tabs: [
            { instance_id: "nowin", tab_id: 1, window_id: 71, url: "https://a/", title: "a" },
            { instance_id: "nowin", tab_id: 2, window_id: 90, url: "https://b/", title: "b" },
            { instance_id: "nowin", tab_id: 3, window_id: 90, url: "https://c/", title: "c" },
            // A foreign tab of ANOTHER instance must not leak into the choice.
            { instance_id: "prox", tab_id: 4, window_id: 55, url: "https://d/", title: "d" },
          ],
        }),
      },
      focus: (opts) => {
        bodies.push(JSON.parse(opts.body));
        return { status: 200, body: { ok: true } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    // The row is offered, not disabled.
    const row = store.statusRows.value.find((r) => r.id === "nowin");
    expect(row.raisable).toBe(true);

    const res = await store.raiseInstance("nowin");
    expect(res).toEqual({ ok: true });
    expect(counts.focus).toBe(1);
    // Window 90 holds two tabs, 71 holds one => 90. Never 55 (another instance).
    expect(bodies[0]).toEqual({ instance: "nowin", windowId: 90 });
  });

  it("stays non-actionable when the instance has no window at all", async () => {
    // The honest null: connected, unfocused AND no mirrored tabs => nothing to raise.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: fleet() }, // tabs: []
      focus: () => ({ status: 200, body: { ok: true } }),
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    expect(store.statusRows.value.find((r) => r.id === "nowin").raisable).toBe(false);
    expect(await store.raiseInstance("nowin")).toEqual({ ok: false, nonActionable: true });
    expect(counts.focus ?? 0).toBe(0); // nothing was sent (the mock counts on first call)
  });

  it("POSTs /api/focus with {instance, windowId} and NO tabId, reports ok", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: fleet() },
      focus: (opts) => {
        bodies.push(JSON.parse(opts.body));
        return { status: 200, body: { ok: true } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    const res = await store.raiseInstance("prox");
    expect(res).toEqual({ ok: true });
    expect(counts.focus).toBe(1);
    expect(bodies[0]).toEqual({ instance: "prox", windowId: 12 });
    expect(bodies[0].tabId).toBeUndefined();
  });

  it("statusRows mark raisable only for foreign, connected, windowed instances", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({ state: { status: 200, body: fleet() } });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    const byId = Object.fromEntries(store.statusRows.value.map((r) => [r.id, r]));
    expect(byId.prox.raisable).toBe(true);      // foreign, connected, has a window
    expect(byId.me.raisable).toBe(false);        // OWN browser — no-op
    expect(byId.closed.raisable).toBe(false);    // disconnected — unreachable
    expect(byId.nowin.raisable).toBe(false);     // no focused_window_id — nothing to raise
  });

  it("a non-raisable row (own / disconnected / no window) sends NO request", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({ state: { status: 200, body: fleet() } });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    expect((await store.raiseInstance("me")).nonActionable).toBe(true);
    expect((await store.raiseInstance("nowin")).nonActionable).toBe(true);
    expect(counts.focus).toBeUndefined();
  });

  it("a 409 no_window re-fetches state (the mirror's window is gone)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: fleet() },
      focus: { status: 409, body: { ok: false, error: "no_window", refetch: true } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    const before = counts.state;
    const res = await store.raiseInstance("prox");
    expect(res).toEqual({ ok: false, refetch: true });
    expect(counts.state).toBe(before + 1);
  });
});

// --- raise an OWN window (§62 item 4): click a window header -------------------
describe("raise own window (§62 item 4)", () => {
  it("focuses the window and touches NO tab (active tab unchanged, newtab stays)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();

    await store.raiseOwnWindow(6);
    expect(env.calls.winUpdate).toEqual([[6, { focused: true }]]);
    // The whole point of item 4: no tab is activated and the newtab is not closed.
    expect(env.calls.tabUpdate).toEqual([]);
    expect(env.calls.tabRemove).toEqual([]);
  });

  it("a null windowId is a no-op (a search-only pseudo group)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({ state: undefined });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.raiseOwnWindow(null);
    expect(env.calls.winUpdate).toEqual([]);
  });
});

// --- run all rules now (§62 item 5) -------------------------------------------
describe("run all rules now (§62 item 5)", () => {
  it("POSTs /api/run_pass {run_all:true} and surfaces the status", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
      runPass: (opts) => {
        bodies.push(JSON.parse(opts.body));
        return { status: 200, body: { status: "ok" } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    const res = await store.runRulesNow();
    expect(res).toEqual({ ok: true, status: "ok" });
    expect(counts.runPass).toBe(1);
    expect(bodies[0]).toEqual({ run_all: true });
    expect(store.runNowResult.value).toEqual({ status: "ok" });
  });

  it("offline: no request, an explicit error", async () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const { fetchFn, counts } = makeFetch({ state: undefined });
    const store = storeWith(env, fetchFn);
    await store.init();
    const res = await store.runRulesNow();
    expect(res.offline).toBe(true);
    expect(counts.runPass).toBeUndefined();
  });
});

// --- the deferred-pass plan (§7) ---------------------------------------------
describe("pending_plan (§7)", () => {
  it("normalizes the plan so the human sees WHAT the confirm will do", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    store.applyState({
      instances: [],
      tabs: [],
      quick_links: [],
      resume_pending: true,
      pending_plan: {
        since: 1234,
        plan: {
          relocations: 12,
          phase_b_completions: 3,
          closures: 40,
          deferred: { no_home: 2, unreachable: 1 },
          relocation_examples: [{ url: "https://a", from: "x", to: "y" }],
        },
      },
    });
    expect(store.pendingPlan.value).toMatchObject({
      since: 1234,
      relocations: 12,
      phaseBCompletions: 3,
      closures: 40,
      deferred: 3,
    });
    expect(store.pendingPlan.value.examples).toHaveLength(1);
  });

  it("accepts a bare plan object and reads a missing plan as null", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    store.applyState({ quick_links: [], pending_plan: { relocations: 5, closures: 1 } });
    expect(store.pendingPlan.value).toMatchObject({ relocations: 5, closures: 1, deferred: 0 });
    store.applyState({ quick_links: [] });
    expect(store.pendingPlan.value).toBe(null);
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

// --- stop / start (§7) --------------------------------------------------------
describe("stop status (§7)", () => {
  it("surfaces stopped_at + resume_pending from state (offline-first, via applyState)", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    // applyState is the single writer and runs from the CACHE too — a cached stop
    // renders with no network (§7 "видимость обязательна").
    store.applyState({
      instances: [],
      tabs: [],
      quick_links: [],
      stopped_at: 5_000_000,
      resume_pending: true,
    });
    expect(store.stoppedAt.value).toBe(5_000_000);
    expect(store.resumePending.value).toBe(true);

    // A pre-stop state (no key) reads as "running", never undefined.
    store.applyState({ instances: [], tabs: [], quick_links: [] });
    expect(store.stoppedAt.value).toBe(null);
    expect(store.resumePending.value).toBe(false);
  });

  it("carries the plan's total + threshold through normalization (§7 threshold gate)", () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    store.applyState({
      instances: [],
      tabs: [],
      quick_links: [],
      resume_pending: true,
      pending_plan: {
        since: 1,
        plan: { relocations: 18, phase_b_completions: 3, closures: 7, total: 25, threshold: 20 },
      },
    });
    expect(store.pendingPlan.value.total).toBe(25);
    expect(store.pendingPlan.value.threshold).toBe(20);
  });

  it("pauseCurator POSTs /api/pause and reflects the stop stamp", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      pausePost: { status: 200, body: { stopped_at: 9_000_000 } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh(); // online: base/token set

    const res = await store.pauseCurator();
    expect(res.ok).toBe(true);
    expect(counts.pausePost).toBe(1);
    expect(store.stoppedAt.value).toBe(9_000_000);
  });

  it("resumeCurator DELETEs /api/pause, clears the stop, and re-fetches state", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], stopped_at: null } },
      pauseDelete: { status: 200, body: { resumed: true, pass: { status: "no_ready_instances" } } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();
    store.stoppedAt.value = 9_000_000; // pretend a stop was armed

    const before = counts.state;
    const res = await store.resumeCurator();
    expect(res.ok).toBe(true);
    expect(counts.pauseDelete).toBe(1);
    expect(store.stoppedAt.value).toBe(null);
    expect(counts.state).toBe(before + 1); // manual start re-fetches the truth
  });

  it("resumeCurator holds `resuming` for the whole in-flight DELETE and refuses re-entry", async () => {
    // DELETE /api/pause spans the WHOLE confirming pass (tens of seconds on a big
    // fleet). A second DELETE fired meanwhile lands after the stop has cleared
    // server-side, i.e. as a confirm of a plan the human never saw — so the flag
    // must cover the full request and a re-entrant call must send nothing.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      pauseDelete: async () => {
        await gate; // park the DELETE in flight until the test releases it
        return { status: 200, body: { resumed: true } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    expect(store.resuming.value).toBe(false);
    const first = store.resumeCurator(); // not awaited: the DELETE is parked
    expect(store.resuming.value).toBe(true);

    // Re-entry while in flight: no-op, and crucially NO second DELETE goes out.
    const second = await store.resumeCurator();
    expect(second.ok).toBe(false);
    expect(counts.pauseDelete).toBe(1);

    release();
    expect((await first).ok).toBe(true);
    expect(store.resuming.value).toBe(false); // cleared in finally
    expect(counts.pauseDelete).toBe(1);
  });

  it("resumeCurator does NOT optimistically clear the latch — refresh() owns it", async () => {
    // A DELETE while stopped runs a NORMAL-gated pass: the latch may survive or
    // re-arm, so the client must not guess it away — the refresh that follows
    // reports it truthfully (here: the server says the plan is still over the
    // threshold).
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [],
          tabs: [],
          quick_links: [],
          stopped_at: null,
          resume_pending: true,
          pending_plan: { since: 1, plan: { relocations: 30, closures: 5, total: 35, threshold: 20 } },
        },
      },
      pauseDelete: { status: 200, body: { resumed: true } },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();
    store.stoppedAt.value = 9_000_000; // pretend a stop was armed under the latch

    const res = await store.resumeCurator();
    expect(res.ok).toBe(true);
    expect(store.stoppedAt.value).toBe(null); // the verb itself lifted the stop
    // The latch is whatever the refresh said — NOT cleared by the resume click.
    expect(store.resumePending.value).toBe(true);
    expect(store.pendingPlan.value).toMatchObject({ relocations: 30, closures: 5 });
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

  it("a tab closed since the render RE-QUERIES and re-renders, never fails silently", async () => {
    // tabs.update REJECTS for a dead tab id. The click handler's promise has no owner,
    // so without this catch the page just sits there listing a tab that is gone — §10
    // requires "перезапросить состояние и перерисоваться, а не молчать".
    let closed = false;
    const env = makeChrome({
      tabs: [{ id: 5, windowId: 3, url: "https://gone", title: "Gone" }],
      messages: {},
      tabUpdateThrowsFor: 5,
    });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn, {
      closeSelf: () => {
        closed = true;
      },
    });
    await store.init();
    expect(store.ownTabs.value).toHaveLength(1);
    env.setTabs([]); // the tab really is gone by the time we re-query

    await expect(store.jumpOwn({ tab_id: 5, window_id: 3 })).resolves.toBeUndefined();

    expect(store.ownTabs.value).toEqual([]); // re-queried and re-rendered
    expect(store.fallbackMessage.value).toContain("закрыта");
    expect(closed).toBe(false); // the newtab is NOT closed on a failed jump
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

// --- enrollment status banner (§7) — sourced from getConnectionState, not /api/state ---
describe("enroll status banner (§7)", () => {
  it("acc 13: a fresh profile with NO address shows 'адрес не настроен'", async () => {
    const env = makeChrome({
      tabs: [],
      credential: null, // nothing enrolled: the SW has no address and no secret
      messages: {
        get_identity: { instanceId: "me" },
        get_connection_state: { enrollState: "needs-enroll", hasAddress: false },
      },
    });
    const { fetchFn } = makeFetch({ instance: {}, state: undefined });
    const store = storeWith(env, fetchFn);
    await store.init();
    expect(store.hasAddress.value).toBe(false);
    expect(store.enrollStatus.value).toEqual({ state: "no-address", label: "адрес не настроен" });
  });

  it("a bundled instance.json serviceUrl can NO LONGER fake 'address configured'", async () => {
    // The old fallback set hasAddress from `config.serviceUrl` (and took a `config.token`
    // that has not existed since the shared token was removed). A fleet bundle shipping a
    // bootstrap address therefore switched the "адрес не настроен" banner OFF on profiles
    // that had no secret and could talk to nobody. hasAddress now comes from the SW alone.
    const env = makeChrome({
      tabs: [],
      credential: null,
      messages: {
        get_identity: { instanceId: "me" },
        get_connection_state: { enrollState: "needs-enroll", hasAddress: false },
      },
    });
    // instance.json DOES carry a serviceUrl (and even a legacy token) — it must not matter.
    const { fetchFn, counts } = makeFetch({
      instance: { serviceUrl: "wss://bundled.example/", token: "legacy" },
      state: undefined,
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    expect(store.hasAddress.value).toBe(false);
    expect(store.enrollStatus.value.state).toBe("no-address");
    expect(counts.instance).toBeUndefined(); // the page does not even read the file
  });

  it("a REFUSED address is named, not reported as 'not configured'", async () => {
    // The SW speaks wss:// only (loopback ws:// aside) because the raw secret rides that
    // connection. "адрес не настроен" over a field the operator visibly filled in sends
    // them looking in the wrong place.
    const env = makeChrome({
      tabs: [],
      credential: null,
      messages: {
        get_identity: { instanceId: "me" },
        get_connection_state: {
          enrollState: "needs-enroll",
          hasAddress: false,
          addressError: "insecure",
        },
      },
    });
    const store = storeWith(env, makeFetch({ state: undefined }).fetchFn);
    await store.init();
    expect(store.enrollStatus.value.state).toBe("bad-address");
    expect(store.enrollStatus.value.label).toMatch(/wss:\/\//);
  });

  it("shows 'не зарегистрирован' — there is no 'ожидает одобрения' state left", async () => {
    // Enrolment is one step (§6): an enroll_request is accepted or refused on the spot, so
    // a browser is either enrolled or it is not. The SW no longer reports `pending`; if an
    // old worker still does, the banner must not invent a waiting state for it.
    for (const enrollState of ["needs-enroll", "pending"]) {
      const env = makeChrome({
        tabs: [],
        messages: {
          get_identity: { instanceId: "me" },
          get_connection_state: { enrollState, hasAddress: true },
        },
      });
      const store = storeWith(env, makeFetch({}).fetchFn);
      await store.init();
      const label = store.enrollStatus.value && store.enrollStatus.value.label;
      expect(label == null || !label.includes("ожидает")).toBe(true);
    }
  });

  it("a refused enrolment shows WHY, in words, not the wire constant", async () => {
    // This banner and the extension settings are the ONLY places a human sees a refusal:
    // /admin has no list of refused attempts anymore. So it has to say what to change.
    const cases = [
      ["id_taken", "имя уже занято"],
      ["bad_id", "имя не подходит"],
      ["bad_code", "неверный код"],
      ["closed", "окно регистрации закрыто"],
      ["brand_new", "brand_new"], // an unlisted reason is shown raw, never swallowed
    ];
    for (const [reason, expected] of cases) {
      const env = makeChrome({
        tabs: [],
        messages: {
          get_identity: { instanceId: "me" },
          get_connection_state: {
            enrollState: "needs-enroll", hasAddress: true, enrollReject: reason,
          },
        },
      });
      const store = storeWith(env, makeFetch({}).fetchFn);
      await store.init();
      expect(store.enrollStatus.value.state).toBe("needs-enroll");
      expect(store.enrollStatus.value.label).toContain("не зарегистрирован");
      expect(store.enrollStatus.value.label).toContain(expected);
    }
  });

  it("a QUARANTINED instance whose re-registration was rejected shows why, and that a new code is needed", async () => {
    // getEnrollState resolves `quarantined` before needs-enroll (the old secret is still
    // active), so this instance NEVER reports needs-enroll — a reject shown only there is
    // invisible exactly here. And here there is no self-healing: the terminal reject wiped
    // the staged code, so the probe stops asking and the banner would sit on
    // "требуется повторная регистрация" over an attempt that already came back refused.
    const env = makeChrome({
      tabs: [],
      messages: {
        get_identity: { instanceId: "me" },
        get_connection_state: {
          enrollState: "quarantined",
          hasAddress: true,
          enrollReject: "bad_code",
        },
      },
    });
    const store = storeWith(env, makeFetch({}).fetchFn);
    await store.init();
    expect(store.enrollStatus.value.state).toBe("quarantined");
    expect(store.enrollStatus.value.label).toContain("повторная регистрация отклонена");
    expect(store.enrollStatus.value.label).toContain("неверный код");
  });

  it("shows 'отозван' from the SW enrollState (revoked)", async () => {
    const env = makeChrome({
      tabs: [],
      messages: {
        get_identity: { instanceId: "me" },
        get_connection_state: { enrollState: "revoked", hasAddress: true },
      },
    });
    const store = storeWith(env, makeFetch({}).fetchFn);
    await store.init();
    expect(store.enrollStatus.value.label).toBe("отозван");
  });

  it("an approved instance shows NO banner (the normal status rows speak)", async () => {
    const env = makeChrome({
      tabs: [],
      messages: {
        get_identity: { instanceId: "me" },
        get_connection_state: { enrollState: "approved", hasAddress: true },
      },
    });
    const store = storeWith(env, makeFetch({}).fetchFn);
    await store.init();
    expect(store.enrollStatus.value).toBe(null);
  });

  it("uses the SW raw secret as the /api Bearer (slice C / option A) when a credential is served", async () => {
    const env = makeChrome({
      tabs: [],
      messages: {
        get_identity: { instanceId: "me" },
        get_credential: { serviceUrl: "wss://curator/", secret: "rawsecret" },
        get_connection_state: { enrollState: "approved", hasAddress: true },
      },
    });
    let seenAuth = null;
    const { fetchFn } = makeFetch({
      state: (n) => ({ status: 200, body: { instances: [], tabs: [], quick_links: [] } }),
    });
    // Wrap fetch to capture the Authorization header on /api/state. Option A: the Bearer is
    // the RAW secret (the server hashes it), NOT a client-derived sha256.
    const wrapped = async (url, opts) => {
      if (String(url).includes("/api/state")) seenAuth = opts && opts.headers && opts.headers.Authorization;
      return fetchFn(url, opts);
    };
    const store = storeWith(env, wrapped);
    await store.init();
    await store.refresh();
    expect(seenAuth).toBe("Bearer rawsecret");
  });
});

// --- bookmarks + history: local sources, optimistic edits ---------------------
describe("bookmarks and history are LOCAL sources (§10)", () => {
  const TREE = [
    {
      id: "1",
      title: "Панель закладок",
      children: [
        { id: "11", title: "Хабр / Habr", url: "https://habr.com/" },
        { id: "12", title: "MDN", url: "https://developer.mozilla.org/" },
      ],
    },
    { id: "2", title: "Другие закладки", children: [] },
  ];

  function env(extra = {}) {
    return makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      bookmarks: structuredClone(TREE),
      // ONE clock: the store passes its injected `now` into queryHistory, so the "last N
      // days" window chrome is asked for and the "Сегодня"/"Вчера" labels are computed
      // from the same source of time. These stamps are therefore relative to NOW, not to
      // the wall clock — read the history window off a raw Date.now() again and this
      // test's items fall outside it.
      history: [
        { url: "https://a.example/1", title: "A", lastVisitTime: NOW - 1000 },
        { url: "https://b.example/1", title: "B", lastVisitTime: NOW - 2000 },
      ],
      ...extra,
    });
  }

  it("init() reads the bookmark tree and history with NO network", async () => {
    const e = env();
    const { fetchFn } = makeFetch({ state: undefined }); // offline throughout
    const store = storeWith(e, fetchFn);
    await store.init();

    // Leaves and folders are separated; the unnamed chrome root is not a folder.
    expect(store.bookmarks.value.map((b) => b.id)).toEqual(["11", "12"]);
    expect(store.bookmarkFolders.value.map((f) => f.title)).toEqual([
      "Панель закладок",
      "Другие закладки",
    ]);
    expect(store.bookmarks.value[0].parentId).toBe("1");
    // The column groups the leaves under their folder's title.
    expect(store.bookmarkGroups.value[0].title).toBe("Панель закладок");
    expect(store.bookmarkGroups.value[0].items).toHaveLength(2);
    // History is there too, newest first, grouped by day.
    expect(store.history.value).toHaveLength(2);
    expect(store.historyGroups.value[0].items[0].url).toBe("https://a.example/1");
  });

  it("a missing chrome.bookmarks / chrome.history is not an error — the lists stay empty", async () => {
    const e = env({ withoutOptionalApis: true });
    const { fetchFn } = makeFetch({ state: undefined });
    const store = storeWith(e, fetchFn);
    await store.init();

    expect(store.bookmarks.value).toEqual([]);
    expect(store.history.value).toEqual([]);
    expect(store.bookmarkGroups.value).toEqual([]);
  });

  it("the search box filters bookmarks and history with the same local filter", async () => {
    const e = env();
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    store.setSearch("mdn");
    expect(store.filteredBookmarks.value.map((b) => b.id)).toEqual(["12"]);
    store.setSearch("b.example");
    expect(store.filteredHistory.value.map((h) => h.title)).toEqual(["B"]);
  });

  it("addBookmark shows the link IMMEDIATELY and then adopts the real chrome id", async () => {
    const e = env();
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    const pending = store.addBookmark("https://new.example/", "New", "1");
    // Optimistic: on screen before the API call resolves.
    expect(store.bookmarks.value.map((b) => b.url)).toContain("https://new.example/");
    await pending;
    const added = store.bookmarks.value.find((b) => b.url === "https://new.example/");
    expect(added.id).not.toMatch(/^pending:/); // the real id replaced the temp one
    expect(e.calls.bookmarkCreate).toHaveLength(1);
  });

  it("a failed create is ROLLED BACK — the page never shows a link the browser lacks", async () => {
    const e = env({ bookmarkWritesFail: true });
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    const before = store.bookmarks.value.length;
    const res = await store.addBookmark("https://new.example/", "New", "1");
    expect(res).toBe(null);
    expect(store.bookmarks.value).toHaveLength(before);
  });

  it("renameBookmark edits the row first and restores the old title if chrome refuses", async () => {
    const ok = env();
    const okStore = storeWith(ok, makeFetch({ state: undefined }).fetchFn);
    await okStore.init();
    expect(await okStore.renameBookmark({ id: "11" }, "Хабр")).toBe(true);
    expect(okStore.bookmarks.value.find((b) => b.id === "11").title).toBe("Хабр");
    expect(ok.calls.bookmarkUpdate[0]).toEqual(["11", { title: "Хабр" }]);

    const bad = env({ bookmarkWritesFail: true });
    const badStore = storeWith(bad, makeFetch({ state: undefined }).fetchFn);
    await badStore.init();
    expect(await badStore.renameBookmark({ id: "11" }, "Хабр")).toBe(false);
    expect(badStore.bookmarks.value.find((b) => b.id === "11").title).toBe("Хабр / Habr");
  });

  it("deleteBookmark removes the row and puts it back if chrome refuses", async () => {
    const ok = env();
    const okStore = storeWith(ok, makeFetch({ state: undefined }).fetchFn);
    await okStore.init();
    expect(await okStore.deleteBookmark({ id: "11" })).toBe(true);
    expect(okStore.bookmarks.value.map((b) => b.id)).toEqual(["12"]);

    const bad = env({ bookmarkWritesFail: true });
    const badStore = storeWith(bad, makeFetch({ state: undefined }).fetchFn);
    await badStore.init();
    expect(await badStore.deleteBookmark({ id: "11" })).toBe(false);
    // Back in its OWN place, not appended to the end.
    expect(badStore.bookmarks.value.map((b) => b.id)).toEqual(["11", "12"]);
  });

  // The delete rollback must undo THE DELETE, nothing else. A snapshot of the whole
  // array taken before the await and restored after it silently reverts every other
  // edit made in the meantime — and chrome.bookmarks.remove is an await long enough for
  // a rename (or the tree-change listener) to land inside it.
  it("a failed delete does NOT roll back edits made while it was in flight", async () => {
    const e = env({ bookmarkWritesFail: true });
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    const failing = store.deleteBookmark({ id: "11" }); // will be refused by chrome
    // …and while it is in flight, another bookmark is renamed and a third appears
    // (exactly what the chrome.bookmarks listener does when the tree moves).
    store.bookmarks.value = [
      ...store.bookmarks.value.map((b) => (b.id === "12" ? { ...b, title: "MDN Web Docs" } : b)),
      { id: "13", parentId: "1", folder: false, title: "Новая", url: "https://new.example/" },
    ];
    expect(await failing).toBe(false);

    // The deleted row is back…
    expect(store.bookmarks.value.map((b) => b.id)).toEqual(["11", "12", "13"]);
    // …and the concurrent rename + addition SURVIVED.
    expect(store.bookmarks.value.find((b) => b.id === "12").title).toBe("MDN Web Docs");
    expect(store.bookmarks.value.find((b) => b.id === "13").title).toBe("Новая");
  });

  // A rollback must put the row back ONCE. The debounced tree re-read is a macrotask and
  // can fire inside the remove() await — and since the delete FAILED, that re-read still
  // finds the node and restores it on its own.
  it("a failed delete does not duplicate a row the tree re-read already restored", async () => {
    const e = env();
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    // Park the refusal so the re-read demonstrably completes INSIDE the await — that is
    // the window the debounced chrome.bookmarks listener fires in.
    let refuse;
    e.chrome.bookmarks.remove = async () => {
      await new Promise((r) => {
        refuse = r;
      });
      throw new Error("bookmarks.remove failed");
    };

    const failing = store.deleteBookmark({ id: "11" });
    expect(store.bookmarks.value.map((b) => b.id)).toEqual(["12"]); // optimistic removal
    // The re-read lands while the refusal is still in flight, and since the delete did
    // NOT go through it puts "11" back on its own.
    await store.reloadBookmarks();
    expect(store.bookmarks.value.map((b) => b.id)).toEqual(["11", "12"]);
    refuse();
    expect(await failing).toBe(false);

    const ids = store.bookmarks.value.map((b) => b.id);
    expect(ids).toEqual(["11", "12"]); // ONE "11", not two
    expect(ids.filter((id) => id === "11")).toHaveLength(1);
  });

  // An in-flight getTree cannot be cancelled, so it must be FENCED: a page that went away
  // mid-read must not have the answer written into its store.
  it("drops a tree re-read that lands after unwatchBookmarkChanges()", async () => {
    const e = env();
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();
    const before = store.bookmarks.value.map((b) => b.id);

    // Park getTree in flight, then tear the watch down before it answers.
    let release;
    const parked = new Promise((r) => {
      release = r;
    });
    e.chrome.bookmarks.getTree = async () => {
      await parked;
      return [{ id: "0", title: "", children: [{ id: "9", title: "Позже", children: [] }] }];
    };
    store.watchBookmarkChanges({ debounceMs: 1 });
    const reading = store.reloadBookmarks();
    store.unwatchBookmarkChanges();
    release();
    await reading;

    // The store belongs to a page that is gone: its lists are untouched.
    expect(store.bookmarks.value.map((b) => b.id)).toEqual(before);
    expect(store.bookmarkFolders.value.map((f) => f.title)).toEqual([
      "Панель закладок",
      "Другие закладки",
    ]);
  });

  // `javascript:` bookmarklets cannot run from this page — it is an extension page under
  // `script-src 'self'`, so the click is killed by the CSP and does nothing at all.
  it("drops javascript: bookmarklets instead of drawing dead rows", async () => {
    const e = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      bookmarks: [
        {
          id: "1",
          title: "Панель закладок",
          children: [
            { id: "11", title: "Хабр", url: "https://habr.com/" },
            { id: "12", title: "Читалка", url: "javascript:void(document.body.style)" },
            { id: "13", title: "Тоже букмарклет", url: "JavaScript:alert(1)" },
            // The browser strips ASCII whitespace and control characters from INSIDE a
            // url before parsing it, so all three of these are the `javascript:` scheme
            // to Chrome — while a `.trim()` + `^javascript:` test sees none of them and
            // draws three rows that cannot do anything.
            { id: "14", title: "С переводом строки", url: "java\nscript:alert(1)" },
            { id: "15", title: "С табом", url: "java\tscript:alert(1)" },
            { id: "16", title: "С ведущим пробелом", url: "  javascript:alert(1)" },
          ],
        },
      ],
    });
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    expect(store.bookmarks.value.map((b) => b.id)).toEqual(["11"]);
    expect(store.bookmarkGroups.value[0].items).toHaveLength(1);
  });

  // The tree changes under an open newtab: a bookmark deleted through Chrome's own UI
  // must leave this list too, or its row stays clickable and its id stays addressable.
  it("re-reads the tree when chrome reports a bookmark change, and debounces a burst", async () => {
    const e = env();
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();
    const readsAfterInit = e.calls.bookmarkGetTree;

    store.watchBookmarkChanges({ debounceMs: 1 });
    expect(e.bookmarkListenerCount()).toBe(4); // onCreated/onChanged/onRemoved/onMoved

    // Somebody deletes a bookmark in Chrome's bookmark manager…
    e.bookmarkRoot.children[0].children.splice(0, 1); // drop "11"
    // …which fires a BURST of events (a folder delete fires one per node).
    e.bookmarkEvents.onRemoved.emit();
    e.bookmarkEvents.onRemoved.emit();
    e.bookmarkEvents.onChanged.emit();
    await new Promise((r) => setTimeout(r, 20));

    expect(store.bookmarks.value.map((b) => b.id)).toEqual(["12"]);
    // Debounced: three events, ONE re-read of the whole tree.
    expect(e.calls.bookmarkGetTree - readsAfterInit).toBe(1);
  });

  it("unwatchBookmarkChanges detaches every listener (no leak across remounts)", async () => {
    const e = env();
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    store.watchBookmarkChanges({ debounceMs: 1 });
    store.watchBookmarkChanges({ debounceMs: 1 }); // a second call must not double up
    expect(e.bookmarkListenerCount()).toBe(4);

    store.unwatchBookmarkChanges();
    expect(e.bookmarkListenerCount()).toBe(0);

    // A detached page must not react to anything anymore.
    const reads = e.calls.bookmarkGetTree;
    e.bookmarkEvents.onCreated.emit();
    await new Promise((r) => setTimeout(r, 20));
    expect(e.calls.bookmarkGetTree).toBe(reads);
  });

  it("a browser with no chrome.bookmarks can still be watched (no throw, no listeners)", async () => {
    const e = env({ withoutOptionalApis: true });
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    expect(() => store.watchBookmarkChanges({ debounceMs: 1 })).not.toThrow();
    expect(() => store.unwatchBookmarkChanges()).not.toThrow();
  });

  // One clock for the whole page: the history WINDOW and the day LABELS must come from
  // the same source of time, or a test can pin one and not the other.
  it("asks chrome for history against the INJECTED clock, not Date.now()", async () => {
    const e = env();
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    expect(e.calls.historySearch).toHaveLength(1);
    const { startTime } = e.calls.historySearch[0];
    expect(startTime).toBe(NOW - 7 * 24 * 60 * 60 * 1000);
  });

  it("own tabs are grouped into the windows they live in", async () => {
    const e = makeChrome({
      tabs: [
        { id: 1, windowId: 5, url: "https://a.com/1", title: "A1" },
        { id: 2, windowId: 5, url: "https://a.com/2", title: "A2" },
        { id: 3, windowId: 6, url: "https://b.com/1", title: "B1" },
      ],
      messages: { get_identity: { instanceId: "me" } },
    });
    const store = storeWith(e, makeFetch({ state: undefined }).fetchFn);
    await store.init();

    expect(store.tabWindowGroups.value.map((g) => g.windowId)).toEqual([5, 6]);
    // The grouping follows the SEARCH, not the raw tab list.
    store.setSearch("B1");
    expect(store.tabWindowGroups.value).toHaveLength(1);
    expect(store.tabWindowGroups.value[0].windowId).toBe(6);
  });
});
