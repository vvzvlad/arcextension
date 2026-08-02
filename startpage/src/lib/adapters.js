// Thin adapters over chrome.* + fetch + instance.json (§6/§10/§12). Isolated so the
// store can be unit-tested with a fake chrome + fetch, and so the "which globals"
// choices live in one place.
//
// The startpage reads instance.json for the API base + token (exactly like the
// popup, §6 "Откуда берётся конфигурация инстанса") — it does not PERSIST the token
// itself (§6). Identity + connection state come from the SW over runtime.sendMessage.

export const STATE_CACHE_KEY = "stateCache"; // storage.local: { state: StateResponse, cached_at }

// KEEP IN SYNC with extension/src/quicklinks.js `httpBaseFromServiceUrl`: duplicated
// across separate build contexts (SW module vs this Vue page bundle); mirror any change.
// ws://→http://, wss://→https://; trailing slashes trimmed (same mapping as popup).
export function httpBaseFromServiceUrl(serviceUrl) {
  const base = String(serviceUrl || "").replace(/\/+$/, "");
  if (base.startsWith("wss://")) return "https://" + base.slice("wss://".length);
  if (base.startsWith("ws://")) return "http://" + base.slice("ws://".length);
  return base;
}

export async function loadInstanceConfig(chromeApi, fetchFn) {
  const url = chromeApi.runtime.getURL("instance.json");
  const resp = await fetchFn(url);
  return await resp.json();
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

export async function readCache(chromeApi) {
  const got = await chromeApi.storage.local.get(STATE_CACHE_KEY);
  return (got && got[STATE_CACHE_KEY]) || null;
}

export async function writeCache(chromeApi, state, cachedAt) {
  await chromeApi.storage.local.set({ [STATE_CACHE_KEY]: { state, cached_at: cachedAt } });
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
export async function postFocus(fetchFn, base, token, instance, tabId) {
  const resp = await fetchFn(base + "/api/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify({ instance, tabId }),
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
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
