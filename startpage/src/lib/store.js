// The startpage store (§10). Framework state lives here as Vue refs/computeds so
// the whole behaviour — offline-first first paint, optimistic quick links, local
// search, own/foreign jump — is unit-testable without mounting a component.
//
// OFFLINE-FIRST (§10): init() paints from LOCAL sources only (own tabs from
// chrome.tabs.query, foreign groups + quick_links from the storage.local cache);
// THEN refresh() does the background GET /api/state. A fresh profile with NO cache
// still renders (own tabs + empty foreign/quick-link sections) — never blank.

import { computed, ref } from "vue";

import {
  STATE_CACHE_KEY,
  enqueueQuickLinkOp,
  fetchState,
  getIdentity,
  httpBaseFromServiceUrl,
  loadInstanceConfig,
  postFocus,
  queryOwnTabs,
  readCache,
  writeCache,
} from "./adapters.js";
import { instanceStatus } from "./status.js";
import { applyOpToQuickLinks, sortQuickLinks } from "./quicklinks.js";
import { matchesQuery } from "./search.js";

export function createStore(deps = {}) {
  const chromeApi = deps.chromeApi || (typeof chrome !== "undefined" ? chrome : undefined);
  const fetchFn = deps.fetchFn || (typeof fetch !== "undefined" ? fetch.bind(globalThis) : undefined);
  const now = deps.now || (() => Date.now());
  const staleMs = deps.staleMs ?? 3000;

  // --- reactive state -------------------------------------------------------
  const ownInstanceId = ref(null);
  const ownTabs = ref([]);
  const instances = ref([]);
  const foreignTabs = ref([]);
  const quickLinks = ref([]);
  const cachedAt = ref(null);
  const offline = ref(false);
  const search = ref("");
  const fallbackMessage = ref("");

  let base = null;
  let token = null;

  // --- helpers --------------------------------------------------------------
  function applyState(state) {
    instances.value = state.instances || [];
    // Own tabs come from chrome.tabs.query; the client filters its own instance out
    // of the server tab list (§10 "Свои вкладки клиент отфильтровывает сам").
    foreignTabs.value = (state.tabs || []).filter(
      (t) => t.instance_id !== ownInstanceId.value,
    );
    quickLinks.value = sortQuickLinks(state.quick_links || []);
  }

  // --- computed views (search is a LOCAL substring filter, §10) -------------
  const filteredOwnTabs = computed(() =>
    ownTabs.value.filter((t) => matchesQuery(t, search.value)),
  );
  const filteredQuickLinks = computed(() =>
    quickLinks.value.filter((q) => matchesQuery(q, search.value)),
  );
  const foreignGroups = computed(() => {
    const byId = new Map();
    for (const t of foreignTabs.value) {
      if (!matchesQuery(t, search.value)) continue;
      if (!byId.has(t.instance_id)) byId.set(t.instance_id, []);
      byId.get(t.instance_id).push(t);
    }
    const metaById = new Map(instances.value.map((i) => [i.id, i]));
    const groups = [];
    for (const [instanceId, tabs] of byId) {
      const meta = metaById.get(instanceId) || { id: instanceId };
      groups.push({
        instanceId,
        title: meta.title || instanceId,
        status: instanceStatus(meta, now(), staleMs),
        // Offline: a jump to a foreign tab is inactive, and the group is labelled
        // "кэш от <время>" (§10).
        jumpable: !offline.value,
        tabs,
      });
    }
    return groups;
  });
  const statusRows = computed(() =>
    instances.value.map((i) => ({
      id: i.id,
      title: i.title || i.id,
      status: instanceStatus(i, now(), staleMs),
    })),
  );

  // --- lifecycle ------------------------------------------------------------
  async function init() {
    // Identity first so foreign filtering is correct; a failure just leaves own
    // filtering off (own tabs may double-show, acceptable degradation).
    const ident = await getIdentity(chromeApi);
    if (ident && ident.instanceId) ownInstanceId.value = ident.instanceId;

    // Config (base + token) for the background refresh; a failure keeps us offline.
    try {
      const config = await loadInstanceConfig(chromeApi, fetchFn);
      base = httpBaseFromServiceUrl(config.serviceUrl);
      token = config.token;
    } catch {
      base = null;
      token = null;
    }

    // FIRST PAINT — local sources only (§10). Own tabs + the cache; never blank.
    ownTabs.value = await queryOwnTabs(chromeApi).catch(() => []);
    const cache = await readCache(chromeApi).catch(() => null);
    if (cache && cache.state) {
      applyState(cache.state);
      cachedAt.value = cache.cached_at ?? null;
      offline.value = true; // until a live refresh proves otherwise
    } else {
      offline.value = true;
    }
  }

  async function refresh() {
    if (!base || !token) {
      offline.value = true;
      return;
    }
    try {
      const state = await fetchState(fetchFn, base, token);
      applyState(state);
      cachedAt.value = now();
      offline.value = false;
      await writeCache(chromeApi, state, cachedAt.value).catch(() => {});
    } catch {
      // Keep the cached render; foreign groups stay labelled "кэш от <время>" (§10).
      offline.value = true;
    }
  }

  // --- quick links (optimistic, §10) ---------------------------------------
  function addQuickLink(url, title) {
    if (!url) return;
    // Optimistic: edit the shown list IMMEDIATELY (§10) — before any flush.
    quickLinks.value = sortQuickLinks(
      applyOpToQuickLinks(quickLinks.value, { op: "add", url, title }),
    );
    // The SW owns the durable queue + flush (§6 enqueue_quicklink_op); best-effort.
    enqueueQuickLinkOp(chromeApi, { op: "add", url, title });
  }

  function removeQuickLink(link) {
    const op = link.id != null ? { op: "remove", id: link.id } : { op: "remove", url: link.url };
    quickLinks.value = sortQuickLinks(applyOpToQuickLinks(quickLinks.value, op));
    enqueueQuickLinkOp(chromeApi, op);
  }

  // --- jump (§10) -----------------------------------------------------------
  async function jumpOwn(tab) {
    await chromeApi.tabs.update(tab.tab_id, { active: true });
    if (tab.window_id != null) {
      await chromeApi.windows.update(tab.window_id, { focused: true });
    }
    await closeSelf();
  }

  async function closeSelf() {
    if (deps.closeSelf) return deps.closeSelf();
    try {
      const self = await chromeApi.tabs.getCurrent();
      if (self && self.id != null) await chromeApi.tabs.remove(self.id);
    } catch {
      // Best-effort: the jump already happened; a failure to close is harmless.
    }
  }

  async function jumpForeign(instanceId, tab) {
    fallbackMessage.value = "";
    // Offline: a foreign jump is inactive (§10) — the instance is unreachable.
    if (offline.value || !base || !token) {
      fallbackMessage.value = `Переключитесь в ${instanceId} вручную`;
      return;
    }
    const { status, body } = await postFocus(fetchFn, base, token, instanceId, tab.tab_id);
    if (status === 200) return;
    // no_such_tab (or an explicit refetch signal): re-fetch state and re-render —
    // never silent (§10). The stale mirror is the cause; a refresh fixes it.
    if (status === 409 && body && (body.error === "no_such_tab" || body.refetch)) {
      await refresh();
      return;
    }
    // Anything else (unreachable / timeout): the manual fallback stays (§10).
    fallbackMessage.value = `Переключитесь в ${instanceId} вручную`;
  }

  function setSearch(q) {
    search.value = q;
  }

  return {
    // state
    ownInstanceId,
    ownTabs,
    instances,
    foreignTabs,
    quickLinks,
    cachedAt,
    offline,
    search,
    fallbackMessage,
    // views
    filteredOwnTabs,
    filteredQuickLinks,
    foreignGroups,
    statusRows,
    // methods
    init,
    refresh,
    applyState,
    addQuickLink,
    removeQuickLink,
    jumpOwn,
    jumpForeign,
    setSearch,
    // for tests / consumers
    STATE_CACHE_KEY,
  };
}
