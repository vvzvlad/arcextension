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
export const QUEUE_KEY = "quickLinkQueue"; // { key, ops: [...] }

// ws://→http://, wss://→https://; trailing slashes trimmed (same mapping as popup).
export function httpBaseFromServiceUrl(serviceUrl) {
  const base = String(serviceUrl || "").replace(/\/+$/, "");
  if (base.startsWith("wss://")) return "https://" + base.slice("wss://".length);
  if (base.startsWith("ws://")) return "http://" + base.slice("ws://".length);
  return base;
}

// Apply ONE op to a quick-links array (NEW array). Mirrors the server + startpage
// semantics: add appends (url upsert on title), remove by id/url, reorder by id list.
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
  if (q && Array.isArray(q.ops)) return q;
  return { key: null, ops: [] };
}

// Enqueue one op: durable queue + optimistic cache edit. Returns the queue.
export async function enqueueOp(env, op) {
  const q = await readQueue(env);
  // A STABLE Idempotency-Key per accumulation window: generated when the first op
  // lands and kept until a successful flush clears the queue, so retries of the
  // SAME batch dedupe server-side (§10).
  if (!q.key) q.key = env.randomUUID();
  q.ops.push(op);
  await env.storageLocalSet({ [QUEUE_KEY]: q });

  // Optimistic cache edit (§10): the shown quick_links change immediately.
  const cacheGot = await env.storageLocalGet(STATE_CACHE_KEY);
  const wrap = cacheGot && cacheGot[STATE_CACHE_KEY];
  if (wrap && wrap.state) {
    wrap.state.quick_links = applyOpToQuickLinks(wrap.state.quick_links, op);
    await env.storageLocalSet({ [STATE_CACHE_KEY]: wrap });
  }
  return q;
}

// Flush the whole queue to POST /api/quick_links/ops with the stable Idempotency-Key.
// On success: reconcile the cache with the server's authoritative quick_links and
// clear the queue. On any failure: leave the queue for a later retry. Never throws.
export async function flushQueue(env) {
  const q = await readQueue(env);
  if (q.ops.length === 0) return { flushed: false };
  let config;
  try {
    config = await env.getInstanceConfig();
  } catch {
    return { flushed: false, error: "config" };
  }
  const base = httpBaseFromServiceUrl(config.serviceUrl);
  try {
    const resp = await env.fetchFn(base + "/api/quick_links/ops", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + config.token,
        "Idempotency-Key": q.key,
      },
      body: JSON.stringify(q.ops),
    });
    if (!resp.ok) return { flushed: false, error: "http_" + resp.status };
    const body = await resp.json();
    // Reconcile the cache with the server's authoritative list.
    const cacheGot = await env.storageLocalGet(STATE_CACHE_KEY);
    const wrap = (cacheGot && cacheGot[STATE_CACHE_KEY]) || { state: {}, cached_at: env.now() };
    wrap.state = wrap.state || {};
    if (body && Array.isArray(body.quick_links)) {
      wrap.state.quick_links = body.quick_links;
      await env.storageLocalSet({ [STATE_CACHE_KEY]: wrap });
    }
    // Clear the queue (and its key) so the next window gets a fresh Idempotency-Key.
    await env.storageLocalSet({ [QUEUE_KEY]: { key: null, ops: [] } });
    return { flushed: true, quick_links: body && body.quick_links };
  } catch {
    return { flushed: false, error: "network" };
  }
}
