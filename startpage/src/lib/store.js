// The startpage store (§10). Framework state lives here as Vue refs/computeds so
// the whole behaviour — offline-first first paint, optimistic quick links, local
// search, own/foreign jump — is unit-testable without mounting a component.
//
// OFFLINE-FIRST (§10): init() paints from LOCAL sources only (own tabs from
// chrome.tabs.query, foreign groups + quick_links from the storage.local cache);
// THEN refresh() does the background GET /api/state. A fresh profile with NO cache
// still renders (own tabs + empty foreign/quick-link sections) — never blank.

import { computed, ref, shallowRef, toRaw } from "vue";

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
  postMergeWindows,
  postPause,
  previewRule,
  queryOwnTabs,
  readCache,
  readQueuedOps,
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

  // --- the clock (§10) ------------------------------------------------------
  // Every timestamp the server hands us — snapshot_at, last_seen_at, paused_until — is
  // on the SERVER's clock. Comparing them to Date.now() means comparing two clocks: a
  // couple of seconds of laptop drift against a 3 s staleness threshold either paints
  // every instance "зеркало устарело" forever or hides a genuinely half-open socket.
  // `StateResponse.server_now` exists precisely so the client can measure the offset
  // (src/db/state.py: "the client decides … against server_now"), so we do all
  // comparisons in the SERVER scale.
  //
  // `clockTick` is what makes those comparisons REACTIVE: a computed that called a
  // plain now() would be evaluated once and never again, and the labels on an open
  // page would freeze at the moment of the first paint. App.vue ticks it every second.
  const clockTick = ref(now());
  const serverOffset = ref(0);
  function tick() {
    clockTick.value = now();
  }
  // Local ms -> server scale. The offset is measured at RECEIPT, so it under-reads by
  // the response latency (tens of ms) — irrelevant next to a multi-second threshold.
  function serverNow() {
    return clockTick.value + serverOffset.value;
  }
  // Server ms -> local scale, for rendering a server deadline as a wall-clock time.
  function localFromServer(ms) {
    return ms == null ? null : ms - serverOffset.value;
  }

  // --- pause state (§7) — server-wide; rides in the StateResponse ------------
  // `pausedUntil` is the RAW server deadline (ms) — the status bar counts down from it
  // in the SERVER scale (see serverNow above). It renders from the cache too
  // (offline-first): applyState is the single writer, and it runs from both the cache
  // and a live refresh.
  const pausedUntil = ref(null);
  const resumePending = ref(false);
  const pendingPlan = ref(null);
  const pauseError = ref("");
  // A mutating verb refused by the pause gate (423) — §7's "аварийный стоп", NOT a
  // breakage. Carries the deadline and the retry that re-issues the SAME verb with
  // {force:true}, the exception §7 grants to the human's own buttons.
  const pauseBlock = ref(null); // { verb, until, retry }

  // --- quick-link ops the SW has queued but the server has not confirmed -----
  // `pendingOps` is the durable queue read from storage.local; `inFlightOps` are ops
  // this page just handed to the SW whose ack has not come back yet (so a refresh in
  // that window still sees them). Applying an op twice is harmless — add is an upsert,
  // remove and reorder are idempotent — which is what makes the overlap safe.
  //
  // shallowRef, NOT ref: a deep ref hands out a reactive PROXY per element, so
  // `filter(o => o !== op)` never matches the op it was given and an acked op would
  // stay overlaid forever — a stale `remove` would then delete the same url again on
  // every refresh, including one the human re-added. Both lists are replaced wholesale,
  // never mutated in place, so shallow reactivity is all they need.
  const pendingOps = shallowRef([]);
  const inFlightOps = shallowRef([]);
  const mergeResult = ref(null); // { instanceId, merged } | { instanceId, error }

  // --- rules editor state (§8/§10) — needs the network; degrades gracefully -----
  const rules = ref([]);
  const rulesLoaded = ref(false);
  const rulesOffline = ref(false);
  const rulesError = ref("");
  const rulesPreview = ref(null); // last server-side preview (impact before save)

  let base = null;
  let token = null;

  // --- helpers --------------------------------------------------------------
  // Replay the not-yet-confirmed queue on top of a server list. Without this, a
  // GET /api/state that wins the race against the tick flush drops an offline-added
  // link out of the UI and out of the cache — and if connectivity dies again before
  // the flush, it stays invisible for days while the queue still holds it (§10).
  function overlayPending(list) {
    let out = list || [];
    for (const op of [...pendingOps.value, ...inFlightOps.value]) {
      out = applyOpToQuickLinks(out, op);
    }
    return out;
  }

  // The deferred-pass plan (§7 "план выводится в статус-полосу"). Tolerates both the
  // bare plan and the stored `{since, plan}` wrapper so the human sees WHAT the
  // confirm button will actually do, not just that something is pending.
  function normalizePendingPlan(raw) {
    if (!raw || typeof raw !== "object") return null;
    const plan = raw.plan && typeof raw.plan === "object" ? raw.plan : raw;
    const num = (v) => (typeof v === "number" ? v : 0);
    const deferred = plan.deferred && typeof plan.deferred === "object" ? plan.deferred : {};
    return {
      since: typeof raw.since === "number" ? raw.since : null,
      relocations: num(plan.relocations),
      phaseBCompletions: num(plan.phase_b_completions),
      closures: num(plan.closures),
      deferred: Object.values(deferred).reduce((a, b) => a + num(b), 0),
      examples: [
        ...(Array.isArray(plan.relocation_examples) ? plan.relocation_examples : []),
        ...(Array.isArray(plan.closure_examples) ? plan.closure_examples : []),
      ].slice(0, 5),
    };
  }

  function applyState(state) {
    instances.value = state.instances || [];
    // Own tabs come from chrome.tabs.query; the client filters its own instance out
    // of the server tab list (§10 "Свои вкладки клиент отфильтровывает сам").
    foreignTabs.value = (state.tabs || []).filter(
      (t) => t.instance_id !== ownInstanceId.value,
    );
    quickLinks.value = sortQuickLinks(overlayPending(state.quick_links || []));
    // Pause (§7): surface the deadline + the after-expiry click-wait. `undefined`
    // (a pre-pause cache) reads as "not paused" rather than clobbering a live value.
    pausedUntil.value = state.paused_until ?? null;
    resumePending.value = !!state.resume_pending;
    pendingPlan.value = normalizePendingPlan(state.pending_plan);
  }

  // --- computed views (search is a LOCAL substring filter, §10) -------------
  const filteredOwnTabs = computed(() =>
    ownTabs.value.filter((t) => matchesQuery(t, search.value)),
  );
  const filteredQuickLinks = computed(() =>
    quickLinks.value.filter((q) => matchesQuery(q, search.value)),
  );
  // The GROUPED TAB LISTS — deliberately free of the ticking clock. This computed is
  // the expensive one (§10 promises hundreds of rows, and every invalidation makes Vue
  // re-diff the whole v-for), so it must depend only on data that actually changes:
  // the tabs, the search string, the instance titles, offline. `serverNow()` reads
  // `clockTick`, so folding the status label in here rebuilt the entire foreign list
  // once a second.
  const foreignTabGroups = computed(() => {
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
        meta,
        // Offline: a jump to a foreign tab is inactive, and the group is labelled
        // "кэш от <время>" (§10).
        jumpable: !offline.value,
        tabs,
      });
    }
    return groups;
  });
  // The clock-dependent layer on top: cheap (one status per instance) and the ONLY
  // thing the 1 s tick invalidates. Group identity and `tabs` array identity are
  // preserved from foreignTabGroups, so the v-for keys and children are untouched.
  const foreignGroups = computed(() =>
    foreignTabGroups.value.map((g) => ({
      ...g,
      // SERVER scale + the ticking clock: the label must be right despite clock drift
      // AND must keep updating while the page stays open.
      status: instanceStatus(g.meta, serverNow(), staleMs),
    })),
  );
  const statusRows = computed(() =>
    instances.value.map((i) => ({
      id: i.id,
      title: i.title || i.id,
      status: instanceStatus(i, serverNow(), staleMs),
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
    // Read the SW's queue BEFORE applying anything, so the very first paint already
    // shows an offline-added link even if the cache write lost the race.
    pendingOps.value = await readQueuedOps(chromeApi).catch(() => []);
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
    // Read the queue BOTH SIDES of the request and overlay the UNION. Each single read
    // has its own hole, and they are mirror images:
    //   * read only BEFORE — an op enqueued AND acked inside the request window is in
    //     neither `queuedBefore` nor `inFlightOps` (the ack already drained it), so the
    //     link vanishes from the page until some later refresh;
    //   * read only AFTER — the SW's tick flush confirms the batch between the response
    //     and the read, the read comes back empty, and the response (built BEFORE that
    //     flush applied) does not carry the link either — it vanishes from the UI AND
    //     gets written to the cache that way.
    // The union closes both. It can only OVER-apply, and re-applying an op is
    // idempotent: add upserts, remove is stable, reorder is absolute.
    const queuedBefore = await readQueuedOps(chromeApi).catch(() => []);
    try {
      const state = await fetchState(fetchFn, base, token);
      // Measure the clock offset from THIS response (§10): every server timestamp in
      // it is compared against server_now, never against the laptop's clock.
      clockTick.value = now();
      if (typeof state.server_now === "number") {
        serverOffset.value = state.server_now - clockTick.value;
      }
      const queuedAfter = await readQueuedOps(chromeApi).catch(() => []);
      // Oldest first: `queuedBefore` predates `queuedAfter` by construction, and an op
      // present in both is simply applied twice, which is a no-op.
      pendingOps.value = [...queuedBefore, ...queuedAfter];
      applyState(state);
      cachedAt.value = now();
      offline.value = false;
      // Cache the state with the pending ops ALREADY APPLIED — the cache is what the
      // next cold open paints from, and writing the bare server list there would undo
      // the SW's optimistic edit (§10).
      //
      // toRaw is REQUIRED, not cosmetic: `quickLinks.value` is a Vue reactive Proxy and
      // chrome.storage structured-clones its argument — a Proxy throws DataCloneError,
      // so the whole cache write would silently fail (MEASURED: structuredClone of a
      // reactive array => "could not be cloned").
      await writeCache(
        chromeApi,
        { ...state, quick_links: toRaw(quickLinks.value) },
        cachedAt.value,
      ).catch(() => {});
    } catch {
      // Keep the cached render; foreign groups stay labelled "кэш от <время>" (§10).
      offline.value = true;
    }
  }

  // --- quick links (optimistic, §10) ---------------------------------------
  // Hand the op to the SW (which owns the durable queue + flush, §6
  // enqueue_quicklink_op) and keep it in `inFlightOps` until the SW acks: a refresh
  // landing inside that window must still overlay it, or the link blinks out.
  function enqueue(op) {
    inFlightOps.value = [...inFlightOps.value, op];
    const drop = () => {
      inFlightOps.value = inFlightOps.value.filter((o) => o !== op);
    };
    // Once acked the op is in the SW's durable queue — pendingOps picks it up on the
    // next read, so dropping it here loses nothing.
    Promise.resolve(enqueueQuickLinkOp(chromeApi, op)).then(drop, drop);
  }

  function addQuickLink(url, title) {
    if (!url) return;
    const op = { op: "add", url, title };
    // Optimistic: edit the shown list IMMEDIATELY (§10) — before any flush.
    quickLinks.value = sortQuickLinks(applyOpToQuickLinks(quickLinks.value, op));
    enqueue(op);
  }

  function removeQuickLink(link) {
    const op = link.id != null ? { op: "remove", id: link.id } : { op: "remove", url: link.url };
    quickLinks.value = sortQuickLinks(applyOpToQuickLinks(quickLinks.value, op));
    enqueue(op);
  }

  // --- jump (§10) -----------------------------------------------------------
  async function jumpOwn(tab) {
    fallbackMessage.value = "";
    try {
      await chromeApi.tabs.update(tab.tab_id, { active: true });
      if (tab.window_id != null) {
        await chromeApi.windows.update(tab.window_id, { focused: true });
      }
    } catch {
      // The tab was closed between the render and the click: tabs.update rejects and
      // the click handler's promise has no owner, so the page would just sit there
      // showing a tab that no longer exists. §10 requires the opposite — re-read the
      // LOCAL truth and re-render, never stay silent.
      ownTabs.value = await queryOwnTabs(chromeApi).catch(() => []);
      fallbackMessage.value = "Вкладка уже закрыта — список обновлён";
      return;
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

  async function jumpForeign(instanceId, tab, { force = false } = {}) {
    fallbackMessage.value = "";
    // Offline: a foreign jump is inactive (§10) — the instance is unreachable.
    if (offline.value || !base || !token) {
      fallbackMessage.value = `Переключитесь в ${instanceId} вручную`;
      return;
    }
    const { status, body } = await postFocus(fetchFn, base, token, instanceId, tab.tab_id, {
      force,
    });
    if (status === 200) {
      pauseBlock.value = null;
      return;
    }
    // no_such_tab (or an explicit refetch signal): re-fetch state and re-render —
    // never silent (§10). The stale mirror is the cause; a refresh fixes it.
    if (status === 409 && body && (body.error === "no_such_tab" || body.refetch)) {
      pauseBlock.value = null;
      await refresh();
      return;
    }
    // 423: the pause gate, not a breakage (§7). Say WHY and offer the override the
    // spec grants to the human's own buttons — "переключитесь вручную" here would be
    // a lie about a working system with a deliberate stop armed.
    if (status === 423) {
      pauseBlock.value = {
        verb: "focus",
        until: (body && body.until) ?? pausedUntil.value ?? null,
        retry: () => jumpForeign(instanceId, tab, { force: true }),
      };
      return;
    }
    // Anything else (unreachable / timeout): the manual fallback stays (§10).
    fallbackMessage.value = `Переключитесь в ${instanceId} вручную`;
  }

  // --- merge windows now (§9) -----------------------------------------------
  // §9 promises this button explicitly: the pass folds an instance's windows only
  // after an hour of idleness, and "ждать час не хочется" is a real case.
  async function mergeWindowsNow(instanceId, { force = false } = {}) {
    mergeResult.value = null;
    if (offline.value || !base || !token) {
      offline.value = true;
      mergeResult.value = { instanceId, error: "offline" };
      return { ok: false, offline: true };
    }
    const { status, body } = await postMergeWindows(fetchFn, base, token, instanceId, { force });
    if (status >= 200 && status < 300) {
      pauseBlock.value = null;
      mergeResult.value = { instanceId, merged: (body && body.merged) ?? 0 };
      return { ok: true, merged: mergeResult.value.merged };
    }
    if (status === 423) {
      pauseBlock.value = {
        verb: "merge_windows",
        until: (body && body.until) ?? pausedUntil.value ?? null,
        retry: () => mergeWindowsNow(instanceId, { force: true }),
      };
      return { ok: false, paused: true };
    }
    // 409 + refetch: the service's _CLIENT_ERRORS (src/api/instances.py) — `no_window`
    // ("your picture of the windows is stale") and `busy_dragging` (§9 says explicitly
    // that a drag "провалом не считается"). Neither is a breakage, and §10 requires the
    // page to re-read state and re-render rather than show an error for something that
    // did not break.
    if (status === 409 && body && (body.refetch || body.error === "busy_dragging")) {
      mergeResult.value =
        body.error === "busy_dragging"
          ? { instanceId, retryable: "вкладку держат мышью — попробуйте ещё раз" }
          : { instanceId, retryable: "картина окон устарела — состояние обновлено" };
      await refresh();
      return { ok: false, retryable: true };
    }
    mergeResult.value = { instanceId, error: "HTTP " + status };
    return { ok: false };
  }

  // Re-issue the verb the pause gate refused, this time with {force:true} (§7).
  async function retryForced() {
    const blocked = pauseBlock.value;
    if (!blocked) return { ok: false };
    pauseBlock.value = null;
    return blocked.retry();
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
      pendingPlan.value = null;
      pauseBlock.value = null; // whatever the gate refused is no longer refused
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

  // An impact payload is USABLE only if it actually reports an impact. A body that did
  // not parse, or that carries no numbers, must never be rendered ("Переселений:
  // undefined") and must never arm the confirm gate: the server refuses precisely
  // BECAUSE the impact is non-zero, so showing zeros — or a blank block plus an armed
  // "Подтвердить" button — is the blind confirmation §8 exists to prevent. Same rule
  // the popup applies (extension/pages/popup.js).
  function _usablePreview(body) {
    if (!body || typeof body !== "object") return null;
    if (typeof body.relocations !== "number" && typeof body.closures !== "number") return null;
    return {
      ...body,
      relocations: typeof body.relocations === "number" ? body.relocations : 0,
      closures: typeof body.closures === "number" ? body.closures : 0,
    };
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
    const preview = _usablePreview(body);
    if (!preview) {
      rulesError.value = "не удалось прочитать ответ сервера о влиянии";
      return null;
    }
    rulesPreview.value = preview;
    return preview;
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
      const preview = _usablePreview(body);
      if (!preview) {
        // The server asked for confirmation but we cannot show WHAT is being confirmed.
        // Do NOT arm the gate — an armed button with an empty impact block is exactly
        // the echo-confirmation §8 forbids.
        rulesPreview.value = null;
        rulesError.value =
          "сервер требует подтверждения, но его ответ не удалось прочитать — " +
          "нажмите «Показать влияние»";
        return { ok: false, unreadable: true, error: rulesError.value };
      }
      rulesPreview.value = preview; // surface the impact; caller confirms to proceed
      return { ok: false, needsConfirm: true, preview };
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
    // clock (§10)
    clockTick,
    serverOffset,
    tick,
    serverNow,
    localFromServer,
    // pause state (§7)
    pausedUntil,
    resumePending,
    pendingPlan,
    pauseError,
    pauseBlock,
    // quick-link queue overlay (§10) + merge (§9)
    pendingOps,
    mergeResult,
    // rules editor state
    rules,
    rulesLoaded,
    rulesOffline,
    rulesError,
    rulesPreview,
    // views
    filteredOwnTabs,
    filteredQuickLinks,
    foreignTabGroups,
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
    retryForced,
    // merge windows now (§9)
    mergeWindowsNow,
    // rules editor methods
    loadRules,
    previewRuleDraft,
    saveRuleDraft,
    // for tests / consumers
    STATE_CACHE_KEY,
  };
}
