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
  createBookmark,
  deletePause,
  enqueueQuickLinkOp,
  fetchRules,
  fetchState,
  getConnectionState,
  getCredential,
  getIdentity,
  httpBaseFromServiceUrl,
  postFocus,
  postFocusWindow,
  postPause,
  postRunPass,
  previewRule,
  queryBookmarks,
  queryHistory,
  queryOwnTabs,
  readCache,
  readQueuedOps,
  removeBookmark,
  saveRule,
  updateBookmark,
  watchBookmarks,
  writeCache,
} from "./adapters.js";
import { instanceStatus } from "./status.js";
import { applyOpToQuickLinks, sortQuickLinks } from "./quicklinks.js";
import { matchesQuery } from "./search.js";
import { groupHistoryByDay, groupTabsByWindow, windowOrdinals } from "./bookmarks.js";

// The `addressError` codes the SW reports (extension/src/service-address.js), in the
// language of this page. Only the SW validates — the startpage never re-implements the
// rule, it only names the verdict.
const ADDRESS_ERROR_LABELS = {
  insecure: "адрес без шифрования (ws://) — нужен wss://",
  "http-scheme": "адрес сайта вместо адреса сокета — нужен wss://",
  malformed: "адрес записан неверно — нужен wss://хост",
};
const ADDRESS_ERROR_FALLBACK = "адрес отклонён — нужен wss://хост";

// The `enroll_rejected` reasons (src/ext/protocol.py), in the language of this page. Same
// discipline as the address labels: the SERVICE decides, this only names the verdict. An
// unlisted reason falls through to the raw string rather than being swallowed — a new
// refusal reason must be readable before it is pretty.
const ENROLL_REJECT_LABELS = {
  id_taken: "имя уже занято другим активным браузером",
  bad_id: "имя не подходит: 1-64 символа из A-Z a-z 0-9 . _ -",
  bad_code: "неверный код регистрации",
  closed: "окно регистрации закрыто",
  protocol: "версия протокола не совпала",
};

export function createStore(deps = {}) {
  const chromeApi = deps.chromeApi || (typeof chrome !== "undefined" ? chrome : undefined);
  const fetchFn = deps.fetchFn || (typeof fetch !== "undefined" ? fetch.bind(globalThis) : undefined);
  const now = deps.now || (() => Date.now());

  // --- reactive state -------------------------------------------------------
  const ownInstanceId = ref(null);
  const ownTabs = ref([]);
  const instances = ref([]);
  const foreignTabs = ref([]);
  const quickLinks = ref([]);
  // Browser bookmarks + recent history: LOCAL sources, like own tabs (§10). They feed
  // the favourites and history columns and are read once in init(); neither needs the
  // network, so both are on screen at the first paint even on a fresh profile.
  // `bookmarks` holds the leaves (real links), `bookmarkFolders` the folder nodes the
  // column groups them under.
  const bookmarks = ref([]);
  const bookmarkFolders = ref([]);
  const history = ref([]);
  const cachedAt = ref(null);
  const offline = ref(false);
  const search = ref("");
  const fallbackMessage = ref("");
  // Enrollment (§7): the SW's durable-fact state + whether an address is configured.
  // The status bar shows "не зарегистрирован" / "отозван" / "адрес не настроен" from
  // these; connectivity itself stays with /api/state (offline/instances).
  const enrollState = ref("needs-enroll");
  const hasAddress = ref(false);
  // The reason a CONFIGURED address was refused (§7): the client speaks wss:// only
  // (ws:// on loopback aside), because the raw instance secret rides that connection.
  // Without this the banner would say "адрес не настроен" over a filled-in field.
  const addressError = ref(null);
  // The last enroll_rejected reason (bad_code/closed/id_taken/bad_id/…), so the banner
  // says WHY the browser is not enrolled (§7). There is no "ожидает одобрения" state to
  // be stuck in anymore — an enroll_request is answered on the spot (§6) — so a browser
  // that is not enrolled either never tried or was refused, and the reason is the whole
  // news. It is also the only place a human sees it: /admin lists no refused attempts.
  const enrollReject = ref(null);

  // --- the clock (§10) ------------------------------------------------------
  // Every timestamp the server hands us — snapshot_at, last_seen_at, stopped_at — is
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

  // --- stop state (§7) — server-wide; rides in the StateResponse -------------
  // `stoppedAt` is the RAW server stamp (ms) of when the automation was stopped;
  // null = running. The stop is INDEFINITE — no deadline, no countdown — so the only
  // clock math left is rendering the stamp as a wall-clock time. It renders from the
  // cache too (offline-first): applyState is the single writer, and it runs from both
  // the cache and a live refresh.
  const stoppedAt = ref(null);
  const resumePending = ref(false);
  const pendingPlan = ref(null);
  const pauseError = ref("");
  // In-flight guard for resumeCurator: DELETE /api/pause holds the connection for
  // the WHOLE confirming pass (tens of seconds on a big fleet). A second DELETE
  // fired meanwhile lands after the stop has already cleared server-side, so it
  // arrives as a confirm of the over-threshold plan — executing a plan the human
  // never saw. The UI disables both resume buttons while this is set.
  const resuming = ref(false);
  // A mutating verb refused by the stop gate (423) — §7's "аварийный стоп", NOT a
  // breakage. Carries the stop stamp and the retry that re-issues the SAME verb with
  // {force:true}, the exception §7 grants to the human's own buttons.
  const pauseBlock = ref(null); // { verb, since, retry }

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
  // The "выполнить все правила сейчас" outcome (§62 item 5): { status } | { error }.
  const runNowResult = ref(null);

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

  // The over-threshold plan (§7 "план выводится в статус-полосу"). Tolerates both the
  // bare plan and the stored `{since, plan}` wrapper so the human sees WHAT the
  // confirm button will actually do, not just that something is pending. `total` is
  // the COUNTABLE side (relocations + closures; phase B is exempt and keeps running)
  // and `threshold` the configured MAX_ACTIONS_PER_PASS — together they are the whole
  // reason the latch is armed, so the notice renders both.
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
      total: typeof plan.total === "number" ? plan.total : null,
      threshold: typeof plan.threshold === "number" ? plan.threshold : null,
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
    // Stop (§7): surface the indefinite stop + the over-threshold latch. `undefined`
    // (a pre-stop cache) reads as "running" rather than clobbering a live value.
    stoppedAt.value = state.stopped_at ?? null;
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
  // The two new columns use the SAME local substring filter as the tabs (§10): one
  // search box over everything on the page, no second search language to learn.
  const filteredBookmarks = computed(() =>
    bookmarks.value.filter((b) => matchesQuery(b, search.value)),
  );
  const filteredHistory = computed(() =>
    history.value.filter((h) => matchesQuery(h, search.value)),
  );
  // Bookmarks under their folder headers. Folders whose every child is filtered out
  // disappear with their children — an empty section header is noise while searching.
  const bookmarkGroups = computed(() => {
    const titleById = new Map(bookmarkFolders.value.map((f) => [f.id, f.title]));
    const byFolder = new Map();
    for (const b of filteredBookmarks.value) {
      const key = b.parentId == null ? "" : b.parentId;
      if (!byFolder.has(key)) byFolder.set(key, []);
      byFolder.get(key).push(b);
    }
    return [...byFolder.entries()].map(([id, items]) => ({
      id,
      title: titleById.get(id) || "Закладки",
      items,
    }));
  });
  // Own tabs as WINDOW sections (§9): the window is the unit the curator merges and
  // the human recognises, so the tab column is a list of windows, not one flat pile.
  // The window NUMBER comes from the unfiltered list, so it does not move while the
  // human types in the search box (see windowOrdinals).
  const tabWindowOrdinals = computed(() => windowOrdinals(ownTabs.value));
  const tabWindowGroups = computed(() =>
    groupTabsByWindow(filteredOwnTabs.value, tabWindowOrdinals.value),
  );
  // History as day sections. DELIBERATELY not reading `clockTick`: the day labels
  // change once a day, and depending on the 1 s tick would rebuild the whole history
  // list every second (the same trap foreignTabGroups documents above).
  const historyGroups = computed(() => groupHistoryByDay(filteredHistory.value, now()));
  // The GROUPED TAB LISTS — deliberately free of the ticking clock. This computed is
  // the expensive one (§10 promises hundreds of rows, and every invalidation makes Vue
  // re-diff the whole v-for), so it must depend only on data that actually changes:
  // the tabs, the search string, the instance list, offline. `serverNow()` reads
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
        title: instanceId, // the id IS the name (§6): there is no separate title
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
      status: instanceStatus(g.meta, serverNow()),
    })),
  );
  const statusRows = computed(() =>
    instances.value.map((i) => ({
      id: i.id,
      title: i.id, // the id IS the name (§6)
      status: instanceStatus(i, serverNow()),
      // A "space" is RAISABLE (§62 item 3) only when it is a FOREIGN, connected instance
      // with a known foreground window: raising our own browser from its own newtab is a
      // no-op, a closed instance is unreachable, and a null focused_window_id has no
      // window to raise. Carried here so the template can disable non-actionable rows.
      focusedWindowId: i.focused_window_id ?? null,
      raisable:
        i.id !== ownInstanceId.value &&
        i.connected === true &&
        i.focused_window_id != null,
    })),
  );

  // The OWN-instance enroll banner (§7). Only the non-connected enroll states are
  // surfaced here; an approved instance shows nothing (its connectivity is the normal
  // /api/state status rows). `null` => no banner. acc 13: a fresh profile with no
  // address → "адрес не настроен", sourced from getConnectionState (not /api/state).
  const enrollStatus = computed(() => {
    if (addressError.value) {
      // A REFUSED address, not a missing one. Naming the two apart is the point: the
      // operator typed something, and every screen would otherwise claim the field is
      // empty while the instance sits silent.
      return {
        state: "bad-address",
        label: ADDRESS_ERROR_LABELS[addressError.value] || ADDRESS_ERROR_FALLBACK,
      };
    }
    if (!hasAddress.value) {
      return { state: "no-address", label: "адрес не настроен" };
    }
    switch (enrollState.value) {
      case "revoked":
        return { state: "revoked", label: "отозван" };
      case "quarantined":
        // The reject is surfaced HERE too, not only under needs-enroll: getEnrollState
        // resolves `quarantined` ahead of everything but `revoked`, so a quarantined
        // instance (it still holds a valid old secret) never reports needs-enroll and its
        // refused re-registration would show nothing at all. And this is the path with no
        // self-healing — a terminal reject wipes the staged code, so the probe goes quiet
        // and only the operator moves it. The label says that instead of leaving the
        // banner on "требуется повторная регистрация" over an attempt that already failed.
        return enrollReject.value
          ? {
              state: "quarantined",
              label:
                "повторная регистрация отклонена: " +
                (ENROLL_REJECT_LABELS[enrollReject.value] || enrollReject.value),
            }
          : { state: "quarantined", label: "неизвестный инстанс — требуется повторная регистрация" };
      case "needs-enroll":
        // Not enrolled. WHY, when we know: a refusal is the only signal a human gets.
        return enrollReject.value
          ? {
              state: "needs-enroll",
              label:
                "не зарегистрирован — " +
                (ENROLL_REJECT_LABELS[enrollReject.value] || enrollReject.value),
            }
          : { state: "needs-enroll", label: "не зарегистрирован" };
      default:
        return null; // approved / unknown → the normal status rows speak
    }
  });

  // --- lifecycle ------------------------------------------------------------
  async function init() {
    // Identity first so foreign filtering is correct; a failure just leaves own
    // filtering off (own tabs may double-show, acceptable degradation).
    const ident = await getIdentity(chromeApi);
    if (ident && ident.instanceId) ownInstanceId.value = ident.instanceId;

    // Enroll state + address presence from the SW (durable facts, §7). Read first so
    // the status bar can show "адрес не настроен" / "не зарегистрирован" on the very
    // first paint even before any /api/state round-trip (acc 13).
    const cs = await getConnectionState(chromeApi);
    if (cs) {
      if (cs.enrollState) enrollState.value = cs.enrollState;
      hasAddress.value = !!cs.hasAddress;
      addressError.value = cs.addressError ?? null;
      enrollReject.value = cs.enrollReject ?? null;
    }

    // Config (base + token) for the background refresh; without them the page stays
    // offline-but-rendered. The SW is the ONLY source (§7): the validated address setting
    // + the RAW instance secret (slice C / option A — the /api Bearer IS the raw secret;
    // the server hashes it).
    //
    // There is NO instance.json fallback here on purpose. It used to read `config.token`,
    // a field that does not exist in any bundle anymore (the shared token is gone), so it
    // could only ever set `token = undefined` — and its `if (config.serviceUrl)
    // hasAddress = true` actively LIED: a bundle that ships a bootstrap serviceUrl would
    // switch off the "адрес не настроен" banner on a profile that has no secret and
    // cannot talk to anything. `hasAddress` now comes from the SW alone, which is also
    // the only side that can tell a usable address from a refused one.
    const cred = await getCredential(chromeApi);
    if (cred && cred.serviceUrl && cred.secret) {
      base = httpBaseFromServiceUrl(cred.serviceUrl);
      token = cred.secret;
      hasAddress.value = true;
    } else {
      base = null;
      token = null;
    }

    // FIRST PAINT — local sources only (§10). Own tabs + bookmarks + history + the
    // cache; never blank.
    //
    // All five reads go out AT ONCE. They are INDEPENDENT local reads (chrome.tabs,
    // chrome.bookmarks, chrome.history, two storage.local gets) and awaiting them one
    // after another simply adds their latencies together — on the one code path whose
    // entire purpose is a populated page in the first frame. Each carries its OWN
    // `.catch`, so Promise.all can never reject: a browser that refuses one source
    // (permission removed, older Chrome) still paints everything else, which is the
    // offline-first contract, not a nicety.
    //
    // ORDER OF EFFECTS IS UNCHANGED: nothing is assigned until every read has landed,
    // and the queue is still applied BEFORE the cache is rendered — so the very first
    // paint shows an offline-added quick link even if the SW's cache write lost the
    // race.
    //
    // TODO: assign each source AS IT LANDS instead of waiting for Promise.all. Today the
    // SLOWEST of the five gates all the others — a large bookmark tree (getTree walks the
    // whole profile) or chrome.history.search holds back the tab list, which is the one
    // thing that is always ready first and the one the human actually looks at. Left as
    // is deliberately for now: progressive assignment means five independent first paints
    // to reason about instead of one.
    const [tabs, bookmarkNodes, historyItems, queuedOps, cache] = await Promise.all([
      queryOwnTabs(chromeApi).catch(() => []),
      queryBookmarks(chromeApi).catch(() => []),
      queryHistory(chromeApi, { now }).catch(() => []),
      readQueuedOps(chromeApi).catch(() => []),
      readCache(chromeApi).catch(() => null),
    ]);
    ownTabs.value = tabs;
    // Bookmarks + history are local too: no network, available immediately, and the
    // adapters answer [] when the permission/API is absent, so a browser without them
    // renders two empty columns instead of throwing the first paint away.
    bookmarks.value = bookmarkNodes.filter((n) => !n.folder);
    bookmarkFolders.value = bookmarkNodes.filter((n) => n.folder);
    history.value = historyItems;
    pendingOps.value = queuedOps;
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

  // --- bookmarks (optimistic, like quick links) -----------------------------
  // The list on screen is edited FIRST and the browser is told after, so a rename
  // never waits on an API round-trip. Unlike a quick link there is no durable queue
  // behind this: chrome.bookmarks either takes the edit or it does not, so a failed
  // call ROLLS THE OPTIMISTIC EDIT BACK rather than leaving a change on screen that
  // does not exist in the browser.
  async function addBookmark(url, title, parentId = null) {
    if (!url) return null;
    const tempId = "pending:" + url + ":" + now();
    const entry = { id: tempId, parentId, folder: false, title: title || "", url };
    bookmarks.value = [...bookmarks.value, entry];
    const node = await createBookmark(chromeApi, { parentId, title: entry.title, url });
    if (!node || node.id == null) {
      bookmarks.value = bookmarks.value.filter((b) => b.id !== tempId);
      return null;
    }
    // Adopt the real id/parent so a later rename or delete addresses the right node.
    bookmarks.value = bookmarks.value.map((b) =>
      b.id === tempId
        ? {
            ...b,
            id: String(node.id),
            parentId: node.parentId != null ? String(node.parentId) : b.parentId,
          }
        : b,
    );
    return node;
  }

  async function renameBookmark(bookmark, title) {
    if (!bookmark || bookmark.id == null) return false;
    const id = String(bookmark.id);
    const previous = bookmarks.value.find((b) => b.id === id);
    const before = previous ? previous.title : "";
    const next = String(title == null ? "" : title);
    bookmarks.value = bookmarks.value.map((b) => (b.id === id ? { ...b, title: next } : b));
    const node = await updateBookmark(chromeApi, id, { title: next });
    if (!node) {
      bookmarks.value = bookmarks.value.map((b) => (b.id === id ? { ...b, title: before } : b));
      return false;
    }
    return true;
  }

  async function deleteBookmark(bookmark) {
    if (!bookmark || bookmark.id == null) return false;
    const id = String(bookmark.id);
    // Remember the NODE and WHERE it sat — never a snapshot of the whole array. The
    // await below is long enough for other writers to touch the list (a rename, an add,
    // and now the chrome.bookmarks listener re-reading the tree), and restoring a
    // wholesale snapshot would silently roll THOSE back too: rename a bookmark while a
    // failing delete is in flight and the new title vanishes with no error anywhere.
    // The rollback must be as narrow as the edit was — the same discipline
    // renameBookmark and addBookmark already follow.
    const index = bookmarks.value.findIndex((b) => b.id === id);
    if (index < 0) return false;
    const node = bookmarks.value[index];
    bookmarks.value = bookmarks.value.filter((b) => b.id !== id);
    const ok = await removeBookmark(chromeApi, id);
    if (!ok) {
      // The browser still has it — so must the screen. Splice it back into the CURRENT
      // list at its old position, clamped: concurrent edits may have made the list
      // shorter than it was.
      //
      // …unless it is ALREADY back. reloadBookmarks() is a macrotask (a debounced
      // timer behind the chrome.bookmarks listeners) and can fire INSIDE this await:
      // the delete did not go through, so the re-read tree still contains this node and
      // puts it back on its own. Splicing then inserts a SECOND copy with the same id —
      // a duplicate row plus Vue's duplicate-`:key` warning, from a rollback whose whole
      // job was to leave the list exactly as it was.
      if (bookmarks.value.some((b) => b.id === id)) return false;
      const restored = [...bookmarks.value];
      restored.splice(Math.min(index, restored.length), 0, node);
      bookmarks.value = restored;
      return false;
    }
    return true;
  }

  // --- the bookmark tree changes under an open page -------------------------
  // A newtab lives for hours. Bookmarks added or deleted through Chrome's own UI must
  // land here too, or the column drifts into a list of rows that lead nowhere and whose
  // rename/delete buttons address ids the browser has already forgotten.
  //
  // DEBOUNCED: a folder deletion, a drag, or a bookmark import fires one event PER NODE,
  // and re-reading the whole tree per event turns a routine tidy-up into a re-read storm.
  // The listeners are deliberately dumb — they do not try to patch the list from the
  // event payload, they just say "the tree moved"; one authoritative re-read is cheaper
  // to get right than four incremental mutation paths.
  let stopBookmarkWatch = null;
  let bookmarkReloadTimer = null;
  // Bumped by unwatchBookmarkChanges(). A re-read that was ALREADY IN FLIGHT cannot be
  // cancelled — chrome.bookmarks.getTree has no abort — so it is fenced instead: the
  // read snapshots the generation, and a bump while it was awaiting means the page is
  // gone and its answer must be dropped. Without this the tree lands in a store nothing
  // owns anymore, and every remount pays for a write into the previous page's state.
  let bookmarkWatchGeneration = 0;

  async function reloadBookmarks() {
    const generation = bookmarkWatchGeneration;
    const nodes = await queryBookmarks(chromeApi).catch(() => null);
    if (!nodes) return; // a failed re-read keeps what is on screen; never blanks it
    if (generation !== bookmarkWatchGeneration) return; // unwatched mid-read: drop it
    bookmarks.value = nodes.filter((n) => !n.folder);
    bookmarkFolders.value = nodes.filter((n) => n.folder);
  }

  function watchBookmarkChanges({ debounceMs = 250 } = {}) {
    unwatchBookmarkChanges();
    const schedule = () => {
      if (typeof setTimeout !== "function") {
        reloadBookmarks();
        return;
      }
      if (bookmarkReloadTimer != null) clearTimeout(bookmarkReloadTimer);
      bookmarkReloadTimer = setTimeout(() => {
        bookmarkReloadTimer = null;
        reloadBookmarks();
      }, debounceMs);
    };
    stopBookmarkWatch = watchBookmarks(chromeApi, schedule);
    return unwatchBookmarkChanges;
  }

  // MUST be called when the page goes away (App.vue's onUnmounted): a live listener
  // holds this whole store alive, and a pending timer would re-read into a dead one.
  function unwatchBookmarkChanges() {
    // Fences any re-read that is already awaiting getTree (see reloadBookmarks).
    bookmarkWatchGeneration += 1;
    if (bookmarkReloadTimer != null && typeof clearTimeout === "function") {
      clearTimeout(bookmarkReloadTimer);
    }
    bookmarkReloadTimer = null;
    if (stopBookmarkWatch) stopBookmarkWatch();
    stopBookmarkWatch = null;
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

  // Click a window-title header (§62 item 4): raise THIS browser's own window to the
  // foreground and change NOTHING inside it — the active tab must stay the active tab.
  // Unlike jumpOwn this NEVER calls tabs.update and NEVER closeSelf(): the newtab stays
  // open and no tab is activated, so a plain windows.update({focused}) is the whole op.
  async function raiseOwnWindow(windowId) {
    fallbackMessage.value = "";
    if (windowId == null) return;
    try {
      await chromeApi.windows.update(windowId, { focused: true });
    } catch {
      // The window was closed between the render and the click: re-read the LOCAL truth
      // and re-render rather than sit silent (§10), same discipline as jumpOwn.
      ownTabs.value = await queryOwnTabs(chromeApi).catch(() => []);
      fallbackMessage.value = "Окно уже закрыто — список обновлён";
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
    // 423: the stop gate, not a breakage (§7). Say WHY and offer the override the
    // spec grants to the human's own buttons — "переключитесь вручную" here would be
    // a lie about a working system with a deliberate stop armed.
    if (status === 423) {
      pauseBlock.value = {
        verb: "focus",
        since: (body && body.since) ?? stoppedAt.value ?? null,
        retry: () => jumpForeign(instanceId, tab, { force: true }),
      };
      return;
    }
    // Anything else (unreachable / timeout): the manual fallback stays (§10).
    fallbackMessage.value = `Переключитесь в ${instanceId} вручную`;
  }

  // --- raise a space (§62 item 3) -------------------------------------------
  // Click a foreign instance's status row to bring ITS browser to the foreground,
  // touching nothing inside it. Mirrors jumpForeign's error handling but posts
  // {windowId} (focus_window) instead of {tabId} (focus_tab). A non-raisable row (own
  // browser, disconnected, or no known focused window) is a no-op — the template also
  // disables it, and looking the id up here means a null windowId is never sent.
  async function raiseInstance(instanceId, { force = false } = {}) {
    fallbackMessage.value = "";
    const inst = instances.value.find((i) => i.id === instanceId);
    const windowId = inst ? inst.focused_window_id : null;
    if (windowId == null || instanceId === ownInstanceId.value) {
      return { ok: false, nonActionable: true };
    }
    if (offline.value || !base || !token) {
      fallbackMessage.value = `Переключитесь в ${instanceId} вручную`;
      return { ok: false, offline: true };
    }
    const { status, body } = await postFocusWindow(
      fetchFn, base, token, instanceId, windowId, { force }
    );
    if (status === 200) {
      pauseBlock.value = null;
      return { ok: true };
    }
    // no_window / refetch: the mirror's window is gone — re-read state and re-render.
    if (status === 409 && body && (body.error === "no_window" || body.refetch)) {
      pauseBlock.value = null;
      await refresh();
      return { ok: false, refetch: true };
    }
    // 423: the stop gate. Offer the human's own {force:true} override (§7).
    if (status === 423) {
      pauseBlock.value = {
        verb: "focus",
        since: (body && body.since) ?? stoppedAt.value ?? null,
        retry: () => raiseInstance(instanceId, { force: true }),
      };
      return { ok: false, paused: true };
    }
    fallbackMessage.value = `Переключитесь в ${instanceId} вручную`;
    return { ok: false };
  }

  // --- run all rules now (§62 item 5) ---------------------------------------
  // POST /api/run_pass {run_all:true}: run one curator pass immediately, executing even
  // an over-threshold plan in this one click (the server's run_all bypasses the
  // MAX_ACTIONS_PER_PASS latch). The outcome (the pass status) is surfaced briefly the
  // way the old merge button surfaced its result. Offline it no-ops with a note.
  async function runRulesNow() {
    runNowResult.value = null;
    if (offline.value || !base || !token) {
      offline.value = true;
      runNowResult.value = { error: "offline" };
      return { ok: false, offline: true };
    }
    const { status, body } = await postRunPass(fetchFn, base, token, { runAll: true });
    if (status >= 200 && status < 300) {
      runNowResult.value = { status: (body && body.status) || "ok" };
      // The pass may have changed the mirror (relocations/closes) and the latch — re-read.
      await refresh();
      return { ok: true, status: runNowResult.value.status };
    }
    runNowResult.value = { error: "HTTP " + status };
    return { ok: false };
  }

  // Re-issue the verb the stop gate refused, this time with {force:true} (§7).
  async function retryForced() {
    const blocked = pauseBlock.value;
    if (!blocked) return { ok: false };
    pauseBlock.value = null;
    return blocked.retry();
  }

  function setSearch(q) {
    search.value = q;
  }

  // --- stop / start (§7) ----------------------------------------------------
  // Names kept as pause*/resume* — the HTTP path is still /api/pause and renaming
  // would ripple through every consumer for zero behaviour. Semantics are stop/start:
  // POST stops indefinitely (no deadline), DELETE starts + runs a confirming pass.
  // The buttons need the network (a live mutating verb). Offline they no-op with a
  // note; the stop ROW still renders from the cache regardless.
  async function pauseCurator() {
    pauseError.value = "";
    if (!base || !token) {
      offline.value = true;
      pauseError.value = "offline";
      return { ok: false, offline: true };
    }
    const { status, body } = await postPause(fetchFn, base, token);
    if (status >= 200 && status < 300 && body) {
      stoppedAt.value = body.stopped_at ?? stoppedAt.value;
      return { ok: true };
    }
    pauseError.value = "pause failed: HTTP " + status;
    return { ok: false };
  }

  async function resumeCurator() {
    // Re-entry is a no-op while a resume is in flight: the DELETE spans the whole
    // pass, and a second one sent after the stop cleared server-side would arrive
    // as an unseen-plan confirm (see `resuming` above).
    if (resuming.value) return { ok: false };
    pauseError.value = "";
    if (!base || !token) {
      offline.value = true;
      pauseError.value = "offline";
      return { ok: false, offline: true };
    }
    resuming.value = true;
    try {
      const { status } = await deletePause(fetchFn, base, token);
      if (status >= 200 && status < 300) {
        // The server started the automation and ran a pass; reflect only what the
        // verb itself decided: the stop is lifted, and whatever the stop gate
        // refused is no longer refused. The latch (resumePending/pendingPlan) is
        // NOT cleared optimistically — a DELETE while stopped runs a NORMAL-gated
        // pass, so the latch may survive or re-arm; the refresh() below reports it
        // truthfully instead of this side guessing.
        stoppedAt.value = null;
        pauseBlock.value = null;
        await refresh();
        return { ok: true };
      }
      pauseError.value = "resume failed: HTTP " + status;
      return { ok: false };
    } finally {
      resuming.value = false;
    }
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
    bookmarks,
    bookmarkFolders,
    history,
    cachedAt,
    offline,
    search,
    fallbackMessage,
    // enrollment (§7)
    enrollState,
    hasAddress,
    addressError,
    enrollReject,
    // clock (§10)
    clockTick,
    serverOffset,
    tick,
    serverNow,
    localFromServer,
    // stop state (§7)
    stoppedAt,
    resumePending,
    pendingPlan,
    pauseError,
    pauseBlock,
    resuming,
    // quick-link queue overlay (§10) + run-now outcome (§62 item 5)
    pendingOps,
    runNowResult,
    // rules editor state
    rules,
    rulesLoaded,
    rulesOffline,
    rulesError,
    rulesPreview,
    // views
    filteredOwnTabs,
    filteredQuickLinks,
    filteredBookmarks,
    filteredHistory,
    bookmarkGroups,
    tabWindowGroups,
    historyGroups,
    foreignTabGroups,
    foreignGroups,
    statusRows,
    enrollStatus,
    invalidRules,
    // methods
    init,
    refresh,
    applyState,
    addQuickLink,
    removeQuickLink,
    addBookmark,
    renameBookmark,
    deleteBookmark,
    reloadBookmarks,
    watchBookmarkChanges,
    unwatchBookmarkChanges,
    jumpOwn,
    jumpForeign,
    raiseInstance,
    raiseOwnWindow,
    setSearch,
    // stop/start methods (§7)
    pauseCurator,
    resumeCurator,
    retryForced,
    // run all rules now (§62 item 5)
    runRulesNow,
    // rules editor methods
    loadRules,
    previewRuleDraft,
    saveRuleDraft,
    // for tests / consumers
    STATE_CACHE_KEY,
  };
}
