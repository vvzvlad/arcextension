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
export const QUEUE_KEY = "quickLinkQueue"; // { ops: [...] }

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

async function readQueue(env) {
  const got = await env.storageLocalGet(QUEUE_KEY);
  const q = got && got[QUEUE_KEY];
  // Tolerate the legacy `{key, ops}` shape: the Idempotency-Key is now minted per
  // flush (see flushQueue), so only the ops carry over.
  if (q && Array.isArray(q.ops)) return { ops: q.ops };
  return { ops: [] };
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
}

// Enqueue one op: durable queue (serialized) + optimistic cache edit. Returns the queue.
export async function enqueueOp(env, op) {
  const q = await runExclusiveQueue(env, (queue) => {
    queue.ops.push(op);
    return queue;
  });

  // Optimistic cache edit (§10): the shown quick_links change immediately. This is a
  // SEPARATE storage key (best-effort mirror), independent of the durable queue.
  const cacheGot = await env.storageLocalGet(STATE_CACHE_KEY);
  const wrap = cacheGot && cacheGot[STATE_CACHE_KEY];
  if (wrap && wrap.state) {
    wrap.state.quick_links = applyOpToQuickLinks(wrap.state.quick_links, op);
    await env.storageLocalSet({ [STATE_CACHE_KEY]: wrap });
  }
  return q;
}

// Put claimed ops back at the FRONT of the queue (they are older than anything
// enqueued during the failed POST) so a later flush retries them (§10). Serialized.
function restoreClaimed(env, claimedOps) {
  return runExclusiveQueue(env, (queue) => {
    queue.ops = [...claimedOps, ...queue.ops];
    return queue;
  });
}

// Flush the queue to POST /api/quick_links/ops. Never throws.
//
// The claim is ATOMIC (serialized link): it reads the current ops, mints a FRESH
// Idempotency-Key, and clears the queue in the SAME link. So ops enqueued DURING the
// POST below land in the now-empty queue — never swept into this batch nor cleared by
// it — and a grown batch can never reuse a consumed key (which the server's
// `idempotency_key_seen` would skip whole, silently dropping the newer op, §10). On
// success: reconcile the cache. On any failure: put the claimed ops back for a retry.
export async function flushQueue(env) {
  const claim = await runExclusiveQueue(env, (queue) => {
    if (queue.ops.length === 0) return null;
    const claimedOps = queue.ops;
    queue.ops = []; // clear atomically WITH the claim (same serialized link)
    return { claimedOps, key: env.randomUUID() };
  });
  if (!claim) return { flushed: false };

  let config;
  try {
    config = await env.getInstanceConfig();
  } catch {
    await restoreClaimed(env, claim.claimedOps);
    return { flushed: false, error: "config" };
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
      body: JSON.stringify(claim.claimedOps),
    });
    if (!resp.ok) {
      await restoreClaimed(env, claim.claimedOps);
      return { flushed: false, error: "http_" + resp.status };
    }
    const body = await resp.json();
    // Reconcile the cache with the server's authoritative list. The queue was already
    // cleared at claim time; ops enqueued during the POST stay for the next flush.
    const cacheGot = await env.storageLocalGet(STATE_CACHE_KEY);
    const wrap = (cacheGot && cacheGot[STATE_CACHE_KEY]) || { state: {}, cached_at: env.now() };
    wrap.state = wrap.state || {};
    if (body && Array.isArray(body.quick_links)) {
      wrap.state.quick_links = body.quick_links;
      await env.storageLocalSet({ [STATE_CACHE_KEY]: wrap });
    }
    return { flushed: true, quick_links: body && body.quick_links };
  } catch {
    await restoreClaimed(env, claim.claimedOps);
    return { flushed: false, error: "network" };
  }
}
