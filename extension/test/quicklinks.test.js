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

// `slow: true` models chrome.storage with UNEVEN per-key latency: a real IPC round
// trip is not uniform, and it is precisely the uneven case that lets two
// read-modify-write sequences on the cache key interleave. With uniform delays the
// queue chain accidentally staggers them and a "lost update" test would be vacuous.
function makeEnv({ cache, fetchImpl, config, slow = false } = {}) {
  const store = {};
  if (cache) store[STATE_CACHE_KEY] = cache;
  const gap = (key) =>
    slow ? new Promise((r) => setTimeout(r, key === STATE_CACHE_KEY ? 5 : 0)) : Promise.resolve();
  const env = {
    getInstanceConfig: async () => config || { serviceUrl: "wss://host/", token: "tok" },
    storageLocalGet: async (k) => {
      await gap(k);
      return k in store ? { [k]: structuredClone(store[k]) } : {};
    },
    storageLocalSet: async (obj) => {
      await gap(Object.keys(obj)[0]);
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

  it("writes the DURABLE queue BEFORE the cache mirror (order survives a worker death)", async () => {
    // The worker can die between the two writes. Queue-then-cache leaves an op that is
    // queued but not yet shown (self-heals on the next render); cache-then-queue leaves
    // a link SHOWN but present in no queue — it never reaches the server and the first
    // successful /api/state erases it. Swap the two sets and this reddens.
    const writes = [];
    const { env } = makeEnv({ cache: { state: { quick_links: [] }, cached_at: 1 } });
    const realSet = env.storageLocalSet;
    env.storageLocalSet = async (obj) => {
      writes.push(Object.keys(obj)[0]);
      return realSet(obj);
    };
    await enqueueOp(env, { op: "add", url: "https://q" });
    expect(writes.indexOf(QUEUE_KEY)).toBeLessThan(writes.indexOf(STATE_CACHE_KEY));
  });

  it("two CONCURRENT enqueues both reach the cache (the edit is inside the chain)", async () => {
    // The optimistic cache edit is a read-modify-write of a SECOND storage key. Run it
    // outside the serialized chain and two enqueues fired without awaiting interleave
    // their get/await/set: the second write is built on the pre-first cache and the
    // first link silently vanishes from the render (§10). Both must survive.
    const { env, store } = makeEnv({
      cache: { state: { quick_links: [] }, cached_at: 1 },
      slow: true,
    });
    await Promise.all([
      enqueueOp(env, { op: "add", url: "https://a" }),
      enqueueOp(env, { op: "add", url: "https://b" }),
    ]);
    expect(store[QUEUE_KEY].ops).toHaveLength(2);
    expect(store[STATE_CACHE_KEY].state.quick_links.map((l) => l.url).sort()).toEqual([
      "https://a",
      "https://b",
    ]);
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
    // Queue cleared, the claim released; cache reconciled with the server's list.
    expect(store[QUEUE_KEY].ops).toEqual([]);
    expect(store[QUEUE_KEY].claimed).toBe(null);
    expect(store[STATE_CACHE_KEY].state.quick_links[0].id).toBe(1);
  });

  it("keeps the batch DURABLY claimed when the flush fails (offline)", async () => {
    const { env, store } = makeEnv({
      fetchImpl: async () => {
        throw new Error("network down");
      },
    });
    await enqueueOp(env, { op: "add", url: "https://q" });
    const res = await flushQueue(env);

    expect(res.flushed).toBe(false);
    // The batch is still in DURABLE storage (§10 "оффлайн может длиться днями") — a
    // worker death here must not lose it. Delete it at claim time and this reddens.
    expect(store[QUEUE_KEY].claimed.ops).toEqual([{ op: "add", url: "https://q" }]);
    expect(store[QUEUE_KEY].claimed.key).toBe("uuid-fixed");
  });

  // --- the key must be STABLE across retries of the SAME batch (§10) -----------
  it("retries the SAME batch under the SAME Idempotency-Key", async () => {
    // The failure mode this guards: the POST APPLIED but its response died in the
    // network. A retry under a NEW key is a second apply — `add` overwrites the title,
    // `reorder` undoes a newer arrangement. The server only skips a repeat when the key
    // matches (src/db/quick_links.py). Mint per attempt and posts[1].key differs.
    const posts = [];
    let fail = true;
    const { env, store } = makeEnv({
      cache: { state: { quick_links: [] } },
      fetchImpl: async (_url, opts) => {
        posts.push({ key: opts.headers["Idempotency-Key"], ops: JSON.parse(opts.body) });
        if (fail) throw new Error("network down");
        return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
      },
    });
    let n = 0;
    env.randomUUID = () => "key-" + ++n;

    await enqueueOp(env, { op: "add", url: "https://q", title: "Q" });
    expect((await flushQueue(env)).flushed).toBe(false);
    fail = false;
    expect((await flushQueue(env)).flushed).toBe(true);

    expect(posts).toHaveLength(2);
    expect(posts[1].key).toBe(posts[0].key); // SAME key
    expect(posts[1].ops).toEqual(posts[0].ops); // SAME batch
    expect(store[QUEUE_KEY].claimed).toBe(null); // released only on the 2xx
  });

  it("a batch that GREW after the failed attempt is NOT resent under the consumed key", async () => {
    // The counterpart trap: reusing a key for a LARGER batch makes the server skip the
    // whole batch (`idempotency_key_seen`), silently dropping the new ops. The claimed
    // batch and the live queue must therefore stay separate — the retry carries exactly
    // the original ops, and the newcomer flushes later under a fresh key.
    const posts = [];
    let fail = true;
    const { env } = makeEnv({
      fetchImpl: async (_url, opts) => {
        posts.push({ key: opts.headers["Idempotency-Key"], ops: JSON.parse(opts.body) });
        if (fail) throw new Error("network down");
        return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
      },
    });
    let n = 0;
    env.randomUUID = () => "key-" + ++n;

    await enqueueOp(env, { op: "add", url: "https://op1" });
    await flushQueue(env); // fails => op1 stays claimed under key-1
    await enqueueOp(env, { op: "add", url: "https://op2" }); // arrives AFTER the claim
    fail = false;
    await flushQueue(env); // retry: op1 ONLY, key-1
    await flushQueue(env); // then op2, a fresh key

    expect(posts[1]).toEqual({ key: "key-1", ops: [{ op: "add", url: "https://op1" }] });
    expect(posts[2].key).not.toBe("key-1");
    expect(posts[2].ops).toEqual([{ op: "add", url: "https://op2" }]);
  });

  it("revives a STRANDED claimed batch left by a dead worker (§10)", async () => {
    // Simulate the worker dying mid-POST: storage holds a claimed batch and nothing
    // in the live queue. A flush on the next worker start must resend it verbatim —
    // otherwise nothing ever does, and the ops are lost while the queue looks empty.
    const posts = [];
    const { env, store } = makeEnv({
      fetchImpl: async (_url, opts) => {
        posts.push({ key: opts.headers["Idempotency-Key"], ops: JSON.parse(opts.body) });
        return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
      },
    });
    await env.storageLocalSet({
      [QUEUE_KEY]: {
        ops: [],
        claimed: { ops: [{ op: "add", url: "https://stranded" }], key: "key-from-dead-worker" },
      },
    });

    const res = await flushQueue(env);

    expect(res.flushed).toBe(true);
    expect(posts[0]).toEqual({
      key: "key-from-dead-worker",
      ops: [{ op: "add", url: "https://stranded" }],
    });
    expect(store[QUEUE_KEY].claimed).toBe(null);
  });

  it("re-keys a LEGACY {key, ops} queue instead of reusing its key (it is a SUPERSET)", async () => {
    // Legacy minted the key at the FIRST enqueue and kept appending: `{key: K, ops:
    // [o1..oN]}` is a superset of whatever actually went out under K — a POST may have
    // carried [o1..oM], M<N, applied, and lost its response. Re-sending the WHOLE list
    // under K makes the server's idempotency_key_seen(K) skip the batch whole and answer
    // 2xx, and o(M+1)..oN are lost forever. A fresh key replays the superset instead,
    // which is safe: every op is idempotent under last-write-wins.
    //
    // The multi-op queue is the point — a one-op legacy queue cannot show this.
    const posts = [];
    const { env, store } = makeEnv({
      fetchImpl: async (_url, opts) => {
        posts.push({ key: opts.headers["Idempotency-Key"], ops: JSON.parse(opts.body) });
        return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
      },
    });
    const legacyOps = [
      { op: "add", url: "https://o1" }, // may already have been applied under K
      { op: "add", url: "https://o2" },
      { op: "reorder", order: [2, 1] }, // enqueued AFTER that POST left
    ];
    await env.storageLocalSet({ [QUEUE_KEY]: { key: "legacy-key", ops: legacyOps } });

    const res = await flushQueue(env);

    expect(res.flushed).toBe(true);
    expect(posts).toHaveLength(1);
    expect(posts[0].key).not.toBe("legacy-key"); // NEVER the consumed key
    expect(posts[0].ops).toEqual(legacyOps); // nothing dropped from the superset
    expect(store[QUEUE_KEY].claimed).toBe(null);
  });

  it("a 5xx keeps the batch claimed (the server may have applied it before failing)", async () => {
    const { env, store } = makeEnv({
      fetchImpl: async () => ({ ok: false, status: 500, json: async () => ({}) }),
    });
    await enqueueOp(env, { op: "add", url: "https://q" });
    const res = await flushQueue(env);
    expect(res).toEqual({ flushed: false, error: "http_500" });
    expect(store[QUEUE_KEY].claimed.ops).toHaveLength(1);
  });

  it("a 4xx UN-CLAIMS the batch so it cannot poison the head of the queue", async () => {
    // 401 after a token rotation (or 423 from the pause gate) is produced BEFORE the
    // write: nothing applied. Left claimed, it blocks every later op forever — quick
    // links silently stop. Un-claimed, the ops merge with the newcomers and the whole
    // lot goes out under a fresh key once the refusal is over.
    const posts = [];
    let status = 401;
    const { env, store } = makeEnv({
      fetchImpl: async (_url, opts) => {
        posts.push({ key: opts.headers["Idempotency-Key"], ops: JSON.parse(opts.body) });
        if (status !== 200) return { ok: false, status, json: async () => ({}) };
        return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
      },
    });
    let n = 0;
    env.randomUUID = () => "key-" + ++n;

    await enqueueOp(env, { op: "add", url: "https://old" });
    const refused = await flushQueue(env);
    expect(refused).toMatchObject({ flushed: false, error: "http_401", unclaimed: true });
    // Back in the QUEUE (at the front), not stuck in `claimed`.
    expect(store[QUEUE_KEY].claimed).toBe(null);
    expect(store[QUEUE_KEY].ops).toEqual([{ op: "add", url: "https://old" }]);

    // A new op arrives; once auth is fixed BOTH go out together under a fresh key.
    await enqueueOp(env, { op: "add", url: "https://new" });
    status = 200;
    expect((await flushQueue(env)).flushed).toBe(true);
    expect(posts[1].ops).toEqual([
      { op: "add", url: "https://old" }, // oldest first
      { op: "add", url: "https://new" },
    ]);
    expect(posts[1].key).not.toBe(posts[0].key);
  });

  it("423 (paused) un-claims too — a long pause must not freeze the queue for weeks", async () => {
    const { env, store } = makeEnv({
      fetchImpl: async () => ({ ok: false, status: 423, json: async () => ({ error: "paused" }) }),
    });
    await enqueueOp(env, { op: "add", url: "https://q" });
    const res = await flushQueue(env);
    expect(res.unclaimed).toBe(true);
    expect(store[QUEUE_KEY].claimed).toBe(null);
    expect(store[QUEUE_KEY].ops).toHaveLength(1);
  });

  it("refuses a CONCURRENT flush instead of posting the same key twice", async () => {
    // The worker start-up flush and a tick alarm can fire together: both would read the
    // same claimed slot and POST the same batch under the same key in parallel. Only
    // the server's writer serialization saves that, and the client should not be
    // spending it routinely.
    let releaseFetch;
    const gate = new Promise((r) => (releaseFetch = r));
    const posts = [];
    const { env } = makeEnv({
      fetchImpl: async (_url, opts) => {
        posts.push(opts.headers["Idempotency-Key"]);
        await gate;
        return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
      },
    });
    await enqueueOp(env, { op: "add", url: "https://q" });

    const first = flushQueue(env);
    const second = await flushQueue(env); // while the first is parked on the network
    expect(second).toEqual({ flushed: false, error: "in_flight" });

    releaseFetch();
    expect((await first).flushed).toBe(true);
    expect(posts).toHaveLength(1); // exactly ONE POST went out
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
