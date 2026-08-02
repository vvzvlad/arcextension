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
  deletePause,
  enqueueQuickLinkOp,
  fetchRules,
  fetchState,
  getIdentity,
  httpBaseFromServiceUrl,
  loadInstanceConfig,
  postFocus,
  postPause,
  previewRule,
  queryOwnTabs,
  readCache,
  saveRule,
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

  // --- pause state (§7) — server-wide; rides in the StateResponse ------------
  // `pausedUntil` is the RAW server deadline (ms) — the status bar counts down from it
  // against the LOCAL clock. It renders from the cache too (offline-first): applyState
  // is the single writer, and it runs from both the cache and a live refresh.
  const pausedUntil = ref(null);
  const resumePending = ref(false);
  const pauseError = ref("");

  // --- rules editor state (§8/§10) — needs the network; degrades gracefully -----
  const rules = ref([]);
  const rulesLoaded = ref(false);
  const rulesOffline = ref(false);
  const rulesError = ref("");
  const rulesPreview = ref(null); // last server-side preview (impact before save)

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
    // Pause (§7): surface the deadline + the after-expiry click-wait. `undefined`
    // (a pre-pause cache) reads as "not paused" rather than clobbering a live value.
    pausedUntil.value = state.paused_until ?? null;
    resumePending.value = !!state.resume_pending;
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

  // --- pause / resume (§7) --------------------------------------------------
  // The buttons need the network (a live mutating verb). Offline they no-op with a
  // note; the countdown ROW still renders from the cache regardless.
  async function pauseCurator(minutes = null) {
    pauseError.value = "";
    if (!base || !token) {
      offline.value = true;
      pauseError.value = "offline";
      return { ok: false, offline: true };
    }
    const { status, body } = await postPause(fetchFn, base, token, minutes);
    if (status >= 200 && status < 300 && body) {
      pausedUntil.value = body.paused_until ?? pausedUntil.value;
      return { ok: true };
    }
    pauseError.value = "pause failed: HTTP " + status;
    return { ok: false };
  }

  async function resumeCurator() {
    pauseError.value = "";
    if (!base || !token) {
      offline.value = true;
      pauseError.value = "offline";
      return { ok: false, offline: true };
    }
    const { status } = await deletePause(fetchFn, base, token);
    if (status >= 200 && status < 300) {
      // The server cleared the pause and ran a pass; reflect it + pull fresh state.
      pausedUntil.value = null;
      resumePending.value = false;
      await refresh();
      return { ok: true };
    }
    pauseError.value = "resume failed: HTTP " + status;
    return { ok: false };
  }

  // --- rules editor (§8/§10) ------------------------------------------------
  // The editor is the ONE network-only surface of the offline-first page: it lists
  // rules, previews a rule's whole-pass impact on the CURRENT mirror, and CRUDs
  // through the confirm gate. With no base/token it degrades to a plain "offline"
  // note rather than a broken editor.
  const invalidRules = computed(() => rules.value.filter((r) => r.invalid));

  function _hasNet() {
    if (base && token) return true;
    rulesOffline.value = true;
    return false;
  }

  function _rulePayload(op, draft) {
    if (op === "delete") return { op: "delete", id: draft.id };
    return {
      op,
      id: draft.id,
      pattern: draft.pattern,
      instance_id: draft.instance_id,
      singleton: !!draft.singleton,
      canonical_url: draft.canonical_url || null,
      note: draft.note || null,
    };
  }

  async function loadRules() {
    if (!_hasNet()) {
      rulesError.value = "offline";
      return;
    }
    try {
      rules.value = await fetchRules(fetchFn, base, token);
      rulesLoaded.value = true;
      rulesOffline.value = false;
      rulesError.value = "";
    } catch {
      rulesOffline.value = true;
      rulesError.value = "offline";
    }
  }

  // Preview BEFORE save (§8): the same whole-pass model the confirm gate uses.
  async function previewRuleDraft(op, draft) {
    rulesPreview.value = null;
    rulesError.value = "";
    if (!_hasNet()) {
      rulesError.value = "offline";
      return null;
    }
    const { status, body } = await previewRule(fetchFn, base, token, _rulePayload(op, draft));
    if (status !== 200) {
      rulesError.value = (body && body.detail) || "preview failed";
      return null;
    }
    rulesPreview.value = body;
    return body;
  }

  // Save through the confirm gate (§8): a 409 carries the preview and asks for
  // confirmation; the caller re-invokes with { confirmImpact: true }.
  async function saveRuleDraft(op, draft, { confirmImpact = false } = {}) {
    rulesError.value = "";
    if (!_hasNet()) {
      rulesError.value = "offline";
      return { ok: false, offline: true };
    }
    const rule = _rulePayload(op, draft);
    const { status, body } = await saveRule(fetchFn, base, token, {
      op,
      id: draft.id,
      rule,
      confirmImpact,
    });
    if (status === 409) {
      rulesPreview.value = body; // surface the impact; caller confirms to proceed
      return { ok: false, needsConfirm: true, preview: body };
    }
    if (status >= 200 && status < 300) {
      rulesPreview.value = null;
      await loadRules();
      return { ok: true };
    }
    rulesError.value = (body && body.detail) || "save failed: HTTP " + status;
    return { ok: false, error: rulesError.value };
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
    // pause state (§7)
    pausedUntil,
    resumePending,
    pauseError,
    // rules editor state
    rules,
    rulesLoaded,
    rulesOffline,
    rulesError,
    rulesPreview,
    // views
    filteredOwnTabs,
    filteredQuickLinks,
    foreignGroups,
    statusRows,
    invalidRules,
    // methods
    init,
    refresh,
    applyState,
    addQuickLink,
    removeQuickLink,
    jumpOwn,
    jumpForeign,
    setSearch,
    // pause methods (§7)
    pauseCurator,
    resumeCurator,
    // rules editor methods
    loadRules,
    previewRuleDraft,
    saveRuleDraft,
    // for tests / consumers
    STATE_CACHE_KEY,
  };
}
