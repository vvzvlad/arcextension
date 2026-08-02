// Quick-links offline op queue — OWNED BY THE SERVICE WORKER (§6/§10).
//
// The startpage asks the SW to enqueue an op (runtime.sendMessage
// `enqueue_quicklink_op`). The SW: (1) appends it to the durable queue in
// chrome.storage.local (offline can last DAYS — storage.session would die with the
// browser, §10); (2) OPTIMISTICALLY edits the cached quick_links so the link shows
// immediately even before any flush (§10 "Постановка в очередь сразу правит кэш");
// (3) best-effort flushes the whole queue to POST /api/quick_links/ops with a
// STABLE Idempotency-Key, so a retried flush is a no-op server-side (§10).
//
// Dependency-injected via `env` so it runs under the SW (real chrome/fetch) and
// under vitest (a fake env). Pure helpers are exported for unit tests.

export const STATE_CACHE_KEY = "stateCache"; // { state: StateResponse, cached_at }
// { ops: [...], claimed: {ops:[...], key} | null } — see readQueue/flushQueue.
export const QUEUE_KEY = "quickLinkQueue";

// KEEP IN SYNC: `httpBaseFromServiceUrl` is duplicated in startpage/src/lib/adapters.js
// and `applyOpToQuickLinks` below in startpage/src/lib/quicklinks.js. The two live in
// SEPARATE build contexts (this service-worker module vs the Vue page bundle), so a
// shared import is awkward; any change to either MUST be mirrored in the other.
// ws://→http://, wss://→https://; trailing slashes trimmed (same mapping as popup).
export function httpBaseFromServiceUrl(serviceUrl) {
  const base = String(serviceUrl || "").replace(/\/+$/, "");
  if (base.startsWith("wss://")) return "https://" + base.slice("wss://".length);
  if (base.startsWith("ws://")) return "http://" + base.slice("ws://".length);
  return base;
}

// Apply ONE op to a quick-links array (NEW array). Mirrors the server + startpage
// semantics: add appends (url upsert on title), remove by id/url, reorder by id list.
// KEEP IN SYNC with startpage/src/lib/quicklinks.js `applyOpToQuickLinks` (a separate
// build context — the Vue page bundle — so it is duplicated, not imported).
export function applyOpToQuickLinks(list, op) {
  const links = (list || []).map((l) => ({ ...l }));
  if (!op || typeof op !== "object") return links;
  if (op.op === "add") {
    if (!op.url) return links;
    const existing = links.find((l) => l.url === op.url);
    if (existing) {
      if (op.title !== undefined) existing.title = op.title;
      return links;
    }
    const position = links.reduce((m, l) => Math.max(m, l.position ?? -1), -1) + 1;
    links.push({ id: op.id ?? null, url: op.url, title: op.title ?? null, position, pending: true });
    return links;
  }
  if (op.op === "remove") {
    return links.filter((l) => (op.id != null ? l.id !== op.id : l.url !== op.url));
  }
  if (op.op === "reorder") {
    if (!Array.isArray(op.order)) return links;
    const byId = new Map(links.map((l) => [l.id, l]));
    const ordered = [];
    op.order.forEach((id, i) => {
      const l = byId.get(id);
      if (l) {
        l.position = i;
        ordered.push(l);
        byId.delete(id);
      }
    });
    for (const l of byId.values()) ordered.push(l);
    return ordered;
  }
  return links;
}

// The durable value under QUEUE_KEY is `{ops, claimed}`:
//   ops     — operations not yet handed to any POST;
//   claimed — `{ops, key}` of the batch a flush is CURRENTLY (or was LAST) posting,
//             kept in storage until the server confirms it.
// `claimed` is what makes both the Idempotency-Key and the durability promises hold
// (§10); see flushQueue.
async function readQueue(env) {
  const got = await env.storageLocalGet(QUEUE_KEY);
  const q = got && got[QUEUE_KEY];
  const ops = q && Array.isArray(q.ops) ? q.ops : [];
  const claimed =
    q && q.claimed && Array.isArray(q.claimed.ops) && q.claimed.ops.length > 0 && q.claimed.key
      ? { ops: q.claimed.ops, key: q.claimed.key }
      : null;
  // The LEGACY `{key, ops}` shape reads as "nothing claimed" and its key is DROPPED —
  // deliberately, do not "fix" this by moving the key into `claimed`.
  //
  // The two shapes are not equivalent. Legacy minted the key at the FIRST enqueue and
  // kept appending ops to the same list (`if (!q.key) q.key = uuid(); q.ops.push(op)`),
  // so `{key: K, ops: [o1..oN]}` is a SUPERSET of whatever went out under K: a POST may
  // have carried [o1..oM], M<N, applied, and lost its response. Re-sending [o1..oN]
  // under K makes the server's `idempotency_key_seen(K)` skip the batch WHOLE
  // (apply_ops_with_key, src/db/quick_links.py) and answer 2xx — o(M+1)..oN are dropped
  // forever. That is the "grown batch under a consumed key" this module warns about in
  // flushQueue, hit from the other side.
  //
  // A fresh key re-applies [o1..oN] instead. That is safe by construction: every op is
  // idempotent under last-write-wins (add upserts the title, remove is stable, reorder
  // is absolute), so replaying the superset lands on the same final state.
  return { ops, claimed };
}

// ---------------------------------------------------------------------------
// The single mutation chain for the durable queue key (§10).
//
// ⚠️ ALL access to QUEUE_KEY goes through ONE promise chain (`chain = chain.then(…)`,
// the same discipline as activity-map.js). Each link does get -> mutate -> set and
// RE-READS the queue INSIDE the link, never caching it across an await: chrome.storage
// is async + whole-object last-write-wins, so two interleaved get/await/set silently
// lose an update (MEASURED). Without this, a flush's atomic claim+clear could be
// overwritten by a concurrent enqueue that read the pre-clear queue — resurrecting a
// consumed batch and dropping the newer op after cache eviction (§10 data loss).
// ---------------------------------------------------------------------------
let chain = Promise.resolve();

// Run `fn(queue)` as the next link of the chain. `fn` receives the queue read INSIDE
// the link (never cached), mutates it in place (and may return a value), then the
// mutated queue is written back. A rejected link never poisons the chain.
function runExclusiveQueue(env, fn) {
  const result = chain.then(async () => {
    const queue = await readQueue(env);
    const value = await fn(queue);
    await env.storageLocalSet({ [QUEUE_KEY]: queue });
    return value;
  });
  chain = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

// Test seam: reset the chain between tests (the storage mock is recreated per test).
export function __resetQueueChain() {
  chain = Promise.resolve();
  flushInFlight = false;
}

// Enqueue one op: durable queue + optimistic cache edit, BOTH inside ONE serialized
// link. Returns the queue.
//
// The cache edit belongs in the chain even though it touches a DIFFERENT storage key:
// it is a read-modify-write, so two enqueues a few ms apart (two clicks, or a click
// while the tick flush runs) interleave their get/await/set and the second silently
// overwrites the first's edit — the added link disappears from the rendered cache
// while sitting in the queue (§10 "Постановка в очередь сразу правит кэш").
// ORDER MATTERS: the DURABLE queue is written FIRST, the cache mirror second. The
// worker can die between the two writes, and only one order is survivable — queue then
// cache leaves an op that will be flushed but is not yet shown (self-heals on the next
// render), while cache then queue leaves a link that is SHOWN but exists in no queue:
// it never reaches the server and the first successful /api/state silently erases it.
// (runExclusiveQueue also writes the queue after `fn` returns; that repeat write is the
// same value, so it is a harmless no-op.)
export async function enqueueOp(env, op) {
  return runExclusiveQueue(env, async (queue) => {
    queue.ops.push(op);
    await env.storageLocalSet({ [QUEUE_KEY]: queue });
    const cacheGot = await env.storageLocalGet(STATE_CACHE_KEY);
    const wrap = cacheGot && cacheGot[STATE_CACHE_KEY];
    if (wrap && wrap.state) {
      wrap.state.quick_links = applyOpToQuickLinks(wrap.state.quick_links, op);
      await env.storageLocalSet({ [STATE_CACHE_KEY]: wrap });
    }
    return queue;
  });
}

// Flush the queue to POST /api/quick_links/ops. Never throws.
//
// TWO invariants, both riding on the durable `claimed` slot (§10):
//
//   1. A RETRY RESENDS THE SAME BATCH UNDER THE SAME KEY. The server records the
//      Idempotency-Key and skips a repeat (src/db/quick_links.py) — but only if the
//      key is the same. Minting a fresh key per attempt turns a lost RESPONSE (the
//      POST applied, the reply died in the network) into a DOUBLE APPLY: a repeated
//      `add` overwrites the title, a repeated `reorder` undoes a newer arrangement.
//      So the key is minted WITH the claim and stored beside it.
//   2. THE OPS SURVIVE A SERVICE-WORKER DEATH. Clearing the queue at claim time and
//      restoring it only after the response loses the batch outright if the worker
//      dies (or the browser quits) mid-POST — while §10's "offline may last days"
//      promise rests on that queue. So the claim MOVES the batch into `claimed` in
//      the same storage value instead of deleting it; only a confirmed 2xx removes it,
//      and a flush that finds a stranded `claimed` resends it verbatim.
//
// The claim stays ATOMIC (one serialized link), so ops enqueued DURING the POST land
// in the now-empty `ops` and are never swept into the claimed batch — a GROWN batch
// under a consumed key would be skipped whole by the server, dropping the newer ops.
//
// ⚠️ KNOWN LIMIT — the key's protection is bounded by the server's marker retention:
// `qlkey:*` rows are swept after _DEFAULT_IDEMPOTENCY_RETENTION_DAYS (30 days,
// src/db/retention.py — chosen to swallow a forgotten pause plus a long absence). A
// batch still claimed past that horizon would, on resend, apply a SECOND time. The
// client cannot close this alone: the marker lifetime is the server's. What the client
// does do is stop feeding it — a definitively-refused batch (4xx, including the 423
// pause gate) is un-claimed below instead of sitting claimed for weeks.
export async function flushQueue(env) {
  // ONE flush at a time per worker. Two flushes can easily be triggered together (the
  // worker start-up flush and a tick alarm firing right after it): both would read the
  // same `claimed` slot and POST the SAME batch under the SAME key in parallel. The
  // server does dedupe them, but only via its writer serialization — the client must
  // not be spending that protection routinely. Worker-local by design: the queue's own
  // owner is the worker, and a fresh worker has nothing in flight.
  if (flushInFlight) return { flushed: false, error: "in_flight" };
  flushInFlight = true;
  try {
    return await flushQueueInner(env);
  } finally {
    flushInFlight = false;
  }
}

let flushInFlight = false;

async function flushQueueInner(env) {
  const claim = await runExclusiveQueue(env, (queue) => {
    // A stranded claimed batch (a previous flush that failed, or a worker that died
    // mid-POST) is re-sent VERBATIM, key included — never re-keyed, never merged with
    // whatever accumulated since.
    if (queue.claimed) return queue.claimed;
    if (queue.ops.length === 0) return null;
    const claimed = { ops: queue.ops, key: env.randomUUID() };
    queue.claimed = claimed;
    queue.ops = []; // move (not delete) atomically WITH the claim
    return claimed;
  });
  if (!claim) return { flushed: false };

  // Drop the claimed batch — ONLY on a confirmed server answer. `claimed` is compared
  // by key so a concurrent claim (impossible today, but cheap to be exact about) can
  // never be cleared by an older flush's late success.
  const releaseClaim = () =>
    runExclusiveQueue(env, (queue) => {
      if (queue.claimed && queue.claimed.key === claim.key) queue.claimed = null;
    });

  // Give the batch back to the queue (at the FRONT — it is the oldest) and drop the
  // claim. Only ever called when the server proved it did NOT apply the batch.
  const unclaimToFront = () =>
    runExclusiveQueue(env, (queue) => {
      if (queue.claimed && queue.claimed.key === claim.key) {
        queue.ops = [...queue.claimed.ops, ...queue.ops];
        queue.claimed = null;
      }
    });

  let config;
  try {
    config = await env.getInstanceConfig();
  } catch {
    return { flushed: false, error: "config" }; // batch stays claimed for the retry
  }
  const base = httpBaseFromServiceUrl(config.serviceUrl);
  try {
    const resp = await env.fetchFn(base + "/api/quick_links/ops", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + config.token,
        "Idempotency-Key": claim.key,
      },
      body: JSON.stringify(claim.ops),
    });
    if (!resp.ok) {
      if (resp.status >= 400 && resp.status < 500) {
        // DEFINITIVELY REFUSED => nothing was applied. A 4xx is produced before (or
        // instead of) the write: 401 after a token rotation, 400 on a malformed body,
        // 423 from the pause gate (§7). Keeping such a batch claimed poisons the head of
        // the queue — every later op sits behind a POST that can only fail, so a token
        // rotation or a long pause silently stops quick links entirely. Un-claim it back
        // to the FRONT of the queue (it is older than anything enqueued since): the ops
        // survive, they merge with the newcomers, and the whole lot goes out under a
        // FRESH key once the refusal is over. Safe precisely because nothing applied —
        // this is the one case where re-keying cannot double-apply.
        await unclaimToFront();
        return { flushed: false, error: "http_" + resp.status, unclaimed: true };
      }
      // 5xx: the server may have applied the batch and failed afterwards, so the batch
      // stays claimed and the SAME key is retried. A 2xx is the only proof it took it.
      return { flushed: false, error: "http_" + resp.status };
    }
    const body = await resp.json();
    await releaseClaim();
    // Reconcile the cache with the server's authoritative list, then re-apply the ops
    // still queued (enqueued during this POST) so the rendered cache never regresses.
    await runExclusiveQueue(env, async (queue) => {
      if (!body || !Array.isArray(body.quick_links)) return;
      const cacheGot = await env.storageLocalGet(STATE_CACHE_KEY);
      const wrap = (cacheGot && cacheGot[STATE_CACHE_KEY]) || { state: {}, cached_at: env.now() };
      wrap.state = wrap.state || {};
      let links = body.quick_links;
      for (const op of [...(queue.claimed ? queue.claimed.ops : []), ...queue.ops]) {
        links = applyOpToQuickLinks(links, op);
      }
      wrap.state.quick_links = links;
      await env.storageLocalSet({ [STATE_CACHE_KEY]: wrap });
    });
    return { flushed: true, quick_links: body && body.quick_links };
  } catch {
    return { flushed: false, error: "network" }; // batch stays claimed for the retry
  }
}
