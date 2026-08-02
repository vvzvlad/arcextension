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

  it("still reports a genuinely stale mirror as stale (the offset is not a blanket pass)", async () => {
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
    expect(store.statusRows.value[0].status.state).toBe("stale");
  });

  it("the 1s tick does NOT rebuild the foreign TAB LISTS (§10 promises hundreds of rows)", async () => {
    // The status label needs the ticking clock; the grouped tab lists do not. Folding
    // both into one computed made every second invalidate the whole foreign list and
    // forced Vue to re-diff every v-for row. The tab arrays must keep their identity.
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
      staleMs: 3000,
    });
    await store.init();
    await store.refresh();

    const listBefore = store.foreignTabGroups.value;
    const tabsBefore = listBefore[0].tabs;
    expect(store.foreignGroups.value[0].status.state).toBe("ok");

    localNow = NOW + 60_000;
    store.tick();

    // The clock-dependent layer DID update...
    expect(store.foreignGroups.value[0].status.state).toBe("stale");
    // ...while the expensive grouping was not recomputed at all.
    expect(store.foreignTabGroups.value).toBe(listBefore);
    expect(store.foreignTabGroups.value[0].tabs).toBe(tabsBefore);
  });

  it("the labels RE-EVALUATE as time passes on an open page", async () => {
    // A computed reading a plain now() is evaluated once and frozen: a page left open
    // would keep saying "на связи" about an instance whose mirror aged out.
    let localNow = NOW;
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { ...instanceAt(NOW), server_now: NOW } },
    });
    const store = createStore({
      chromeApi: env.chrome,
      fetchFn,
      now: () => localNow,
      staleMs: 3000,
    });
    await store.init();
    await store.refresh();
    expect(store.statusRows.value[0].status.state).toBe("ok");

    localNow = NOW + 60_000; // a minute passes with no new snapshot
    store.tick();
    expect(store.statusRows.value[0].status.state).toBe("stale");
  });
});

// --- 423: the pause gate is not a breakage (§7) -------------------------------
describe("paused verbs offer the human's force override (§7)", () => {
  it("a 423 jump surfaces the deadline and retries with force:true on demand", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
      focus: (opts, n) => {
        bodies.push(JSON.parse(opts.body));
        return n === 1
          ? { status: 423, body: { error: "paused", until: NOW + 3_600_000 } }
          : { status: 200, body: { ok: true } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    await store.jumpForeign("other", { tab_id: 3 });

    // NOT the useless manual fallback: the reason is named and an override offered.
    expect(store.fallbackMessage.value).toBe("");
    expect(store.pauseBlock.value).toMatchObject({ verb: "focus", until: NOW + 3_600_000 });
    expect(bodies[0].force).toBeUndefined(); // never forced automatically

    await store.retryForced();
    expect(bodies[1]).toMatchObject({ instance: "other", tabId: 3, force: true });
    expect(store.pauseBlock.value).toBe(null);
    expect(counts.focus).toBe(2);
  });
});

// --- merge windows now (§9) ---------------------------------------------------
describe("merge windows now (§9)", () => {
  it("POSTs /api/instances/:id/merge_windows and reports {merged}", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const urls = [];
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
      merge: { status: 200, body: { merged: 4 } },
    });
    const wrapped = async (url, opts) => {
      urls.push(String(url));
      return fetchFn(url, opts);
    };
    const store = storeWith(env, wrapped);
    await store.init();
    await store.refresh();

    const res = await store.mergeWindowsNow("prox");
    expect(res).toEqual({ ok: true, merged: 4 });
    expect(urls.some((u) => u.endsWith("/api/instances/prox/merge_windows"))).toBe(true);
    expect(store.mergeResult.value).toEqual({ instanceId: "prox", merged: 4 });
  });

  it("a 423 merge offers the force override too", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
      merge: (opts, n) => {
        bodies.push(JSON.parse(opts.body));
        return n === 1
          ? { status: 423, body: { error: "paused", until: NOW + 600_000 } }
          : { status: 200, body: { merged: 2 } };
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    const first = await store.mergeWindowsNow("prox");
    expect(first).toEqual({ ok: false, paused: true });
    expect(store.pauseBlock.value.verb).toBe("merge_windows");
    expect(bodies[0].force).toBeUndefined();

    await store.retryForced();
    expect(bodies[1]).toEqual({ force: true });
    expect(store.mergeResult.value).toEqual({ instanceId: "prox", merged: 2 });
  });

  it("a 409 busy_dragging / stale-window RE-FETCHES state and is not shown as a failure", async () => {
    // §9 says busy_dragging "провалом не считается", and the service answers 409+refetch
    // for both it and no_window (_CLIENT_ERRORS in src/api/instances.py). §10 requires a
    // re-read + re-render, not an error message about something that did not break.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: NOW } },
      merge: {
        status: 409,
        body: { ok: false, error: "busy_dragging", message: "dragging", refetch: true },
      },
    });
    const store = storeWith(env, fetchFn);
    await store.init();
    await store.refresh();

    const before = counts.state;
    const res = await store.mergeWindowsNow("prox");

    expect(res).toEqual({ ok: false, retryable: true });
    expect(counts.state).toBe(before + 1); // re-fetched and re-rendered (§10)
    expect(store.mergeResult.value.error).toBeUndefined();
    expect(store.mergeResult.value.retryable).toContain("мышью");
  });

  it("offline: no request, an explicit error", async () => {
    const env = makeChrome({ tabs: [], messages: {} });
    const { fetchFn, counts } = makeFetch({ state: undefined });
    const store = storeWith(env, fetchFn);
    await store.init();
    const res = await store.mergeWindowsNow("prox");
    expect(res.offline).toBe(true);
    expect(counts.merge).toBeUndefined();
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
