import { describe, it, expect, beforeEach } from "vitest";
import {
  applyOpToQuickLinks,
  enqueueOp,
  flushQueue,
  __resetQueueChain,
  QUEUE_KEY,
  STATE_CACHE_KEY,
} from "../src/quicklinks.js";

// The queue mutation chain is a module-level singleton; reset it between tests so a
// lingering link from one test cannot serialize behind another's fresh env/store.
beforeEach(() => __resetQueueChain());

function makeEnv({ cache, fetchImpl, config } = {}) {
  const store = {};
  if (cache) store[STATE_CACHE_KEY] = cache;
  const env = {
    getInstanceConfig: async () => config || { serviceUrl: "wss://host/", token: "tok" },
    storageLocalGet: async (k) => (k in store ? { [k]: structuredClone(store[k]) } : {}),
    storageLocalSet: async (obj) => {
      for (const k of Object.keys(obj)) store[k] = structuredClone(obj[k]);
    },
    fetchFn:
      fetchImpl ||
      (async () => ({ ok: true, json: async () => ({ ok: true, quick_links: [] }) })),
    randomUUID: () => "uuid-fixed",
    now: () => 123,
  };
  return { env, store };
}

// --- pure op application -----------------------------------------------------
describe("applyOpToQuickLinks", () => {
  it("appends an add at the next server-side position", () => {
    const out = applyOpToQuickLinks([{ id: 1, url: "https://a", position: 0 }], {
      op: "add",
      url: "https://b",
      title: "B",
    });
    expect(out.map((l) => [l.url, l.position])).toEqual([
      ["https://a", 0],
      ["https://b", 1],
    ]);
  });
  it("removes by url and reorders by id list", () => {
    const list = [
      { id: 1, url: "https://a", position: 0 },
      { id: 2, url: "https://b", position: 1 },
    ];
    expect(applyOpToQuickLinks(list, { op: "remove", url: "https://a" }).map((l) => l.url)).toEqual([
      "https://b",
    ]);
    expect(applyOpToQuickLinks(list, { op: "reorder", order: [2, 1] }).map((l) => l.id)).toEqual([
      2, 1,
    ]);
  });
});

// --- enqueue: durable queue + optimistic cache edit (§10) --------------------
describe("enqueueOp (§10)", () => {
  it("queues the op and edits the cached quick_links optimistically", async () => {
    const { env, store } = makeEnv({ cache: { state: { quick_links: [] }, cached_at: 1 } });
    await enqueueOp(env, { op: "add", url: "https://q", title: "Q" });

    // The durable queue carries ops only; the Idempotency-Key is minted per flush.
    expect(store[QUEUE_KEY].ops).toEqual([{ op: "add", url: "https://q", title: "Q" }]);
    // Optimistic: the cached quick_links show the new link BEFORE any flush (§10).
    expect(store[STATE_CACHE_KEY].state.quick_links.map((l) => l.url)).toContain("https://q");
  });

  it("accumulates multiple enqueues into the ops list", async () => {
    const { env, store } = makeEnv({});
    await enqueueOp(env, { op: "add", url: "https://a" });
    await enqueueOp(env, { op: "add", url: "https://b" });
    expect(store[QUEUE_KEY].ops).toHaveLength(2);
  });
});

// --- flush: POST with Idempotency-Key, reconcile cache, clear queue ----------
describe("flushQueue (§10)", () => {
  it("posts the batch with the stable key, reconciles the cache, and clears the queue", async () => {
    let posted = null;
    const { env, store } = makeEnv({
      cache: { state: { quick_links: [] } },
      fetchImpl: async (url, opts) => {
        posted = { url, opts };
        return {
          ok: true,
          json: async () => ({
            ok: true,
            quick_links: [{ id: 1, url: "https://q", title: "Q", position: 0 }],
          }),
        };
      },
    });
    await enqueueOp(env, { op: "add", url: "https://q", title: "Q" });
    const res = await flushQueue(env);

    expect(res.flushed).toBe(true);
    expect(posted.url).toContain("/api/quick_links/ops");
    expect(posted.opts.headers["Idempotency-Key"]).toBe("uuid-fixed");
    expect(JSON.parse(posted.opts.body)).toEqual([{ op: "add", url: "https://q", title: "Q" }]);
    // Queue cleared; cache reconciled with the server's authoritative list (id set).
    expect(store[QUEUE_KEY].ops).toEqual([]);
    expect(store[STATE_CACHE_KEY].state.quick_links[0].id).toBe(1);
  });

  it("retains the queue when the flush fails (offline)", async () => {
    const { env, store } = makeEnv({
      fetchImpl: async () => {
        throw new Error("network down");
      },
    });
    await enqueueOp(env, { op: "add", url: "https://q" });
    const res = await flushQueue(env);

    expect(res.flushed).toBe(false);
    expect(store[QUEUE_KEY].ops).toHaveLength(1); // claimed op restored for a later retry
  });

  // --- the WARNING: a concurrent enqueue during a flush must not lose the op ---
  it("does not lose an op enqueued concurrently during a flush, and uses a fresh key", async () => {
    // The flush is parked on the network (fetchGate) so an enqueue interleaves DURING
    // its POST. With the serialized claim+clear and a fresh per-flush key: op1 is the
    // ONLY op in the flushed batch, op2 survives in the queue, and the next flush sends
    // op2 with a DISTINCT key. Revert to a non-atomic clear (clear AFTER the POST) and
    // op2 is wiped; reuse a stable key and posts[1].key equals posts[0].key — both redden.
    let releaseFetch;
    const fetchGate = new Promise((r) => (releaseFetch = r));
    const posts = [];
    const { env, store } = makeEnv({
      cache: { state: { quick_links: [] } },
      fetchImpl: async (url, opts) => {
        posts.push({
          key: opts.headers["Idempotency-Key"],
          ops: JSON.parse(opts.body),
        });
        await fetchGate; // hold the flush open across a concurrent enqueue
        return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
      },
    });
    let n = 0;
    env.randomUUID = () => "key-" + ++n; // distinct key per flush claim

    await enqueueOp(env, { op: "add", url: "https://op1" });
    const flushP = flushQueue(env);
    // While the flush is parked on the network, a second op is enqueued.
    await enqueueOp(env, { op: "add", url: "https://op2" });
    releaseFetch();
    const res = await flushP;

    expect(res.flushed).toBe(true);
    // The first POST carried ONLY op1 — op2 was NOT swept into the claimed batch.
    expect(posts[0].ops).toEqual([{ op: "add", url: "https://op1" }]);
    // op2 survived in the queue (not lost, not cleared by the flush it raced).
    expect(store[QUEUE_KEY].ops).toEqual([{ op: "add", url: "https://op2" }]);

    // Flush op2: it goes with a FRESH, distinct Idempotency-Key (never reuses op1's).
    await flushQueue(env);
    expect(posts[1].ops).toEqual([{ op: "add", url: "https://op2" }]);
    expect(posts[1].key).not.toBe(posts[0].key);
  });
});
