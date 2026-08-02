import { describe, it, expect } from "vitest";
import {
  applyOpToQuickLinks,
  enqueueOp,
  flushQueue,
  QUEUE_KEY,
  STATE_CACHE_KEY,
} from "../src/quicklinks.js";

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
  it("queues the op with a stable key and edits the cached quick_links optimistically", async () => {
    const { env, store } = makeEnv({ cache: { state: { quick_links: [] }, cached_at: 1 } });
    await enqueueOp(env, { op: "add", url: "https://q", title: "Q" });

    expect(store[QUEUE_KEY].key).toBe("uuid-fixed");
    expect(store[QUEUE_KEY].ops).toEqual([{ op: "add", url: "https://q", title: "Q" }]);
    // Optimistic: the cached quick_links show the new link BEFORE any flush (§10).
    expect(store[STATE_CACHE_KEY].state.quick_links.map((l) => l.url)).toContain("https://q");
  });

  it("keeps ONE stable Idempotency-Key across multiple enqueues (retry-safe)", async () => {
    const { env, store } = makeEnv({});
    await enqueueOp(env, { op: "add", url: "https://a" });
    await enqueueOp(env, { op: "add", url: "https://b" });
    expect(store[QUEUE_KEY].key).toBe("uuid-fixed");
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
    expect(store[QUEUE_KEY].ops).toHaveLength(1); // kept for a later retry
  });
});
