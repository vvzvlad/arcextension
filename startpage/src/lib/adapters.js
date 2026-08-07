// Thin adapters over chrome.* + fetch (§6/§10/§12). Isolated so the store can be
// unit-tested with a fake chrome + fetch, and so the "which globals" choices live in one
// place.
//
// Identity, the /api base + Bearer and the connection/enroll state ALL come from the SW
// over runtime.sendMessage (§7). The page reads no instance.json: the bundle carries no
// credential anymore, and the SW is the only side that validates the address.

export const STATE_CACHE_KEY = "stateCache"; // storage.local: { state: StateResponse, cached_at }
// KEEP IN SYNC with extension/src/quicklinks.js `QUEUE_KEY` and its value shape
// `{ops, claimed:{ops,key}|null}` — the SW owns the queue, this page only READS it, and
// the two live in separate build contexts (see httpBaseFromServiceUrl below).
export const QUEUE_KEY = "quickLinkQueue";

// KEEP IN SYNC with extension/src/quicklinks.js `httpBaseFromServiceUrl`: duplicated
// across separate build contexts (SW module vs this Vue page bundle); mirror any change.
// ws://→http://, wss://→https://; trailing slashes trimmed (same mapping as popup).
export function httpBaseFromServiceUrl(serviceUrl) {
  const base = String(serviceUrl || "").replace(/\/+$/, "");
  if (base.startsWith("wss://")) return "https://" + base.slice("wss://".length);
  if (base.startsWith("ws://")) return "http://" + base.slice("ws://".length);
  return base;
}

// Own tabs come from chrome.tabs.query — a LOCAL source that needs no network and
// is available on the very first paint (§10 offline-first).
export async function queryOwnTabs(chromeApi) {
  const tabs = await chromeApi.tabs.query({});
  return tabs.map((t) => ({
    tab_id: t.id,
    window_id: t.windowId,
    url: t.url,
    title: t.title,
    fav_icon_url: t.favIconUrl,
    active: !!t.active,
    pinned: !!t.pinned,
    audible: !!t.audible,
  }));
}

// Bookmarks are a LOCAL source too (§10 offline-first): they come from the browser's
// own tree, need no network and are there on the very first paint — same contract as
// queryOwnTabs above. Returned FLAT with `parentId` kept, so the folder hierarchy
// survives without the page having to walk a tree: a node without a `url` IS a folder.
//
// A missing `chrome.bookmarks` (permission removed from the manifest, an older Chrome,
// or a test mock that does not fake it) is not an error — the column simply renders
// empty. The page must never go blank over an absent optional capability.
//
// TODO: bound the list on a large profile (virtualise or cap + "показать все"). A
// profile with thousands of bookmarks renders every leaf into the DOM today.
// TODO: render the folder HIERARCHY (nested paths / indentation). The tree is flattened
// to one section per parent folder, so two same-named folders in different branches are
// indistinguishable.
export async function queryBookmarks(chromeApi) {
  const api = chromeApi && chromeApi.bookmarks;
  if (!api || typeof api.getTree !== "function") return [];
  const tree = await api.getTree();
  const out = [];
  const walk = (nodes, parentId) => {
    for (const n of nodes || []) {
      if (!n) continue;
      const isFolder = !n.url;
      // `javascript:` bookmarklets are DROPPED, not listed. They cannot run from here:
      // this page is an extension page under `script-src 'self'`, so the CSP kills the
      // navigation and the click does nothing at all — a row that silently no-ops is
      // worse than an absent one. (They also carry no host, so they would render as a
      // wall of identical url-fallback rows.) Chrome's own bookmark manager is where
      // they still work.
      //
      // The scheme is compared AFTER stripping C0 controls and spaces from the whole
      // string, not just from its ends. The URL parser removes tabs and newlines from
      // ANYWHERE in the input and trims leading/trailing control characters, so
      // "java\nscript:…" and "javascript:…" are the same scheme to the browser
      // while a `.trim()` + `^javascript:` test sees neither — and the row comes back,
      // dead. (Not an XSS hole: the page's CSP is what stops such a url from doing
      // anything. The filter exists so the column does not draw rows that cannot work.)
      if (!isFolder && /^javascript:/i.test(String(n.url).replace(/[\u0000-\u0020]/g, ""))) {
        continue;
      }
      // The tree's unnamed root(s) are plumbing, not a folder anyone put anything in:
      // descend through them without emitting a nameless section header.
      if (isFolder && !n.title && parentId === null) {
        walk(n.children, null);
        continue;
      }
      out.push({
        id: String(n.id),
        parentId: parentId !== null ? parentId : n.parentId != null ? String(n.parentId) : null,
        folder: isFolder,
        title: n.title || "",
        url: n.url || null,
      });
      if (n.children) walk(n.children, String(n.id));
    }
  };
  walk(tree, null);
  return out;
}

// Recent history — the third column. Local, offline-first and deliberately BOUNDED:
// the newtab shows what the human was just doing, not the whole archive, so it asks
// for a week's worth capped at a screenful-plus. Same missing-API tolerance as
// queryBookmarks.
//
// `now` is INJECTED, never read from the global clock: the same page then computes the
// history window and the "Сегодня"/"Вчера" day labels (groupHistoryByDay) from ONE
// source of time. Two clocks here means a test can fix one and not the other, and the
// suite silently stops describing the shipped behaviour.
export async function queryHistory(chromeApi, { maxResults = 60, days = 7, now = Date.now } = {}) {
  const api = chromeApi && chromeApi.history;
  if (!api || typeof api.search !== "function") return [];
  const startTime = now() - days * 24 * 60 * 60 * 1000;
  const items = await api.search({ text: "", startTime, maxResults });
  return (items || []).map((h) => ({
    url: h.url,
    title: h.title || "",
    lastVisitTime: typeof h.lastVisitTime === "number" ? h.lastVisitTime : null,
  }));
}

// --- bookmark edits ------------------------------------------------------------
// Thin best-effort wrappers: the favourites column edits in place, and a click handler
// must never be handed a rejected promise (same discipline as enqueueQuickLinkOp).
// They answer the created/updated node — or null/false on failure, which is what lets
// the store roll its optimistic edit back instead of showing a change that never
// happened.
export async function createBookmark(chromeApi, { parentId, title, url }) {
  const api = chromeApi && chromeApi.bookmarks;
  if (!api || typeof api.create !== "function") return null;
  try {
    const payload = { title: title || "", url };
    if (parentId != null) payload.parentId = String(parentId);
    return (await api.create(payload)) || null;
  } catch {
    return null;
  }
}

export async function updateBookmark(chromeApi, id, changes) {
  const api = chromeApi && chromeApi.bookmarks;
  if (!api || typeof api.update !== "function") return null;
  try {
    return (await api.update(String(id), changes)) || null;
  } catch {
    return null;
  }
}

export async function removeBookmark(chromeApi, id) {
  const api = chromeApi && chromeApi.bookmarks;
  if (!api || typeof api.remove !== "function") return false;
  try {
    await api.remove(String(id));
    return true;
  } catch {
    return false;
  }
}

// The bookmark tree CHANGES UNDER AN OPEN NEWTAB. This page stays open for hours while
// the human adds and deletes bookmarks through Chrome's own UI, the bookmark bar, or
// another newtab — and a list read once at init() then drifts: a deleted bookmark keeps
// a clickable row that leads nowhere, and a rename/delete issued from here addresses an
// id the browser no longer has. Subscribe to the four mutation events and re-read.
//
// Returns an UNSUBSCRIBE function, and the caller MUST call it on unmount: listeners
// registered against a page-lifetime handler outlive the component otherwise, and every
// remount adds another one (a re-read storm plus a retained closure per mount).
// A browser without chrome.bookmarks (no permission / older Chrome) yields a no-op
// unsubscribe — the same tolerance queryBookmarks has.
const BOOKMARK_EVENTS = ["onCreated", "onChanged", "onRemoved", "onMoved"];

export function watchBookmarks(chromeApi, handler) {
  const api = chromeApi && chromeApi.bookmarks;
  if (!api || typeof handler !== "function") return () => {};
  const attached = [];
  for (const name of BOOKMARK_EVENTS) {
    const event = api[name];
    if (!event || typeof event.addListener !== "function") continue;
    try {
      event.addListener(handler);
      attached.push(event);
    } catch {
      // A revoked permission can make addListener throw; the rest still attach.
    }
  }
  return () => {
    for (const event of attached) {
      try {
        if (typeof event.removeListener === "function") event.removeListener(handler);
      } catch {
        // Nothing to do — the page is going away either way.
      }
    }
    attached.length = 0;
  };
}

export async function readCache(chromeApi) {
  const got = await chromeApi.storage.local.get(STATE_CACHE_KEY);
  return (got && got[STATE_CACHE_KEY]) || null;
}

export async function writeCache(chromeApi, state, cachedAt) {
  await chromeApi.storage.local.set({ [STATE_CACHE_KEY]: { state, cached_at: cachedAt } });
}

// The quick-link ops the SW has NOT yet had confirmed by the server, oldest first
// (§10). `claimed` is the batch of an in-flight/failed POST — it is older than
// anything still in `ops`, so it applies first.
//
// The page must overlay these on the server's quick_links: a GET /api/state can win
// the race against the (up to 60 s) tick flush, and applying its list verbatim would
// erase a link added offline both from the view AND from the cache — invisible for
// days while the queue still holds it (§10 "Постановка в очередь сразу правит кэш").
export async function readQueuedOps(chromeApi) {
  const got = await chromeApi.storage.local.get(QUEUE_KEY);
  const q = got && got[QUEUE_KEY];
  if (!q) return [];
  const claimed = q.claimed && Array.isArray(q.claimed.ops) ? q.claimed.ops : [];
  const ops = Array.isArray(q.ops) ? q.ops : [];
  return [...claimed, ...ops];
}

export async function fetchState(fetchFn, base, token) {
  const resp = await fetchFn(base + "/api/state", {
    headers: { Authorization: "Bearer " + token },
  });
  if (!resp.ok) throw new Error("GET /api/state failed: HTTP " + resp.status);
  return await resp.json();
}

// Foreign jump (§10): the instance activates the tab and raises its own window. A
// no_such_tab / refetch signal tells the page to re-fetch state and re-render.
//
// `force` is §7's ONE exception to the pause gate: while a pause is armed every
// mutating verb answers 423, and the human's own buttons are allowed through with an
// explicit `{force: true}` (recorded server-side as `initiator=user`). It is never
// sent automatically — only after the human is told WHY the jump was refused.
export async function postFocus(fetchFn, base, token, instance, tabId, { force = false } = {}) {
  const payload = { instance, tabId };
  if (force) payload.force = true;
  const resp = await fetchFn(base + "/api/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify(payload),
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  return { status: resp.status, body };
}

// Click a space (§62 item 3): raise a foreign instance's browser to the foreground by
// window id, WITHOUT touching any tab inside it (POST /api/focus with {windowId}). Same
// shape as postFocus — Bearer auth, returns {status, body}, and the same §7 `force`
// pause exception. The server dispatches this to the extension's focus_window verb.
export async function postFocusWindow(
  fetchFn, base, token, instance, windowId, { force = false } = {}
) {
  const payload = { instance, windowId };
  if (force) payload.force = true;
  const resp = await fetchFn(base + "/api/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify(payload),
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  return { status: resp.status, body };
}

// "выполнить все правила сейчас" (§62 item 5): run one curator pass NOW. `runAll`
// forwards to the server's run_all flag, which executes an over-threshold plan in one
// pass instead of latching behind the MAX_ACTIONS_PER_PASS confirm gate. Returns
// {status, body} so the store surfaces the outcome without throwing on a non-2xx.
export async function postRunPass(fetchFn, base, token, { runAll = false } = {}) {
  const resp = await fetchFn(base + "/api/run_pass", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(runAll ? { run_all: true } : {}),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

// «Открыть регистрацию» (§13): arm the enrollment window and get the freshly minted code
// BACK IN THIS PAGE. The code has to travel to the caller — not merely be armed on the
// server — because the startpage copies it to the clipboard inside the same click handler,
// and `navigator.clipboard.writeText` only works while that click's user activation is
// alive. No request body: the window has no parameters (its length is server config).
export async function postEnrollWindow(fetchFn, base, token) {
  const resp = await fetchFn(base + "/api/enroll/window", {
    method: "POST",
    headers: authHeaders(token),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

// Ask the SW (which owns the offline queue, §6/§10) to enqueue a quick-link op.
// Best-effort: the UI has already updated optimistically, so a failed message must
// not throw into the click handler.
export async function enqueueQuickLinkOp(chromeApi, op) {
  try {
    return await chromeApi.runtime.sendMessage({ type: "enqueue_quicklink_op", ...op });
  } catch {
    return null;
  }
}

export async function getIdentity(chromeApi) {
  try {
    return await chromeApi.runtime.sendMessage({ type: "get_identity" });
  } catch {
    return null;
  }
}

// The /api base + Bearer come from the SW now (§7): the address setting + the RAW
// instance secret (slice C / option A — the /api credential IS the raw secret; the server
// hashes it on receipt). The raw secret crosses only SW->page in-process, then the TLS'd
// /api call. Returns { serviceUrl, secret } or null when the channel is down.
export async function getCredential(chromeApi) {
  try {
    return await chromeApi.runtime.sendMessage({ type: "get_credential" });
  } catch {
    return null;
  }
}

// The SW's durable-fact enroll state + whether an address is configured (§7). The
// status bar shows "ожидает одобрения" / "отозван" / "адрес не настроен" from this;
// connectivity itself stays with /api/state.
export async function getConnectionState(chromeApi) {
  try {
    return await chromeApi.runtime.sendMessage({ type: "get_connection_state" });
  } catch {
    return null;
  }
}

// --- stop / start (§7) --------------------------------------------------------
// Stop the automation indefinitely (POST → {stopped_at}) or start it again (DELETE,
// which also runs an immediate confirming pass). The path is still /api/pause — same
// endpoint, new semantics: no duration, no deadline. Bearer-authed like the other
// verbs. Both return {status, body} so the store can react without throwing on a
// non-2xx (e.g. an offline blip).
export async function postPause(fetchFn, base, token) {
  const resp = await fetchFn(base + "/api/pause", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({}),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

export async function deletePause(fetchFn, base, token) {
  const resp = await fetchFn(base + "/api/pause", {
    method: "DELETE",
    headers: authHeaders(token),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

// --- rules editor (§8/§10) ----------------------------------------------------
// The editor needs the network (a live server-side preview + CRUD). Every call is
// Bearer-authed with the instance token, exactly like fetchState/postFocus above.
function authHeaders(token) {
  return { "Content-Type": "application/json", Authorization: "Bearer " + token };
}

export async function fetchRules(fetchFn, base, token) {
  const resp = await fetchFn(base + "/api/rules", {
    headers: { Authorization: "Bearer " + token },
  });
  if (!resp.ok) throw new Error("GET /api/rules failed: HTTP " + resp.status);
  const body = await resp.json();
  return body.rules || [];
}

// Server-side preview (§8): the SAME whole-pass model the confirm gate uses, so the
// human sees the impact BEFORE saving. Returns the preview payload as-is.
export async function previewRule(fetchFn, base, token, payload) {
  const resp = await fetchFn(base + "/api/rules/preview", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

// CRUD (§7 endpoints). A mutating call may come back 409 carrying the preview when it
// needs confirmation (§8); the caller re-submits with confirm_impact:true. Returns
// {status, body} so the store drives the confirm gate without throwing on a 409.
export async function saveRule(fetchFn, base, token, { op, id, rule, confirmImpact }) {
  const body = { ...rule };
  if (confirmImpact) body.confirm_impact = true;
  let url = base + "/api/rules";
  let method = "POST";
  if (op === "update") {
    url = base + "/api/rules/" + id;
    method = "PUT";
  } else if (op === "delete") {
    url = base + "/api/rules/" + id;
    method = "DELETE";
  }
  const resp = await fetchFn(url, { method, headers: authHeaders(token), body: JSON.stringify(body) });
  const parsed = await resp.json().catch(() => null);
  return { status: resp.status, body: parsed };
}
