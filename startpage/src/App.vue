<script>
// The startpage root (§10). SFC => precompiled render function at build time (no
// runtime compiler, no string template) — the whole point of the plugin-vue setup.
//
// Rendering is driven by the store (src/lib/store.js). `deps` is an optional prop so
// tests can inject a fake chrome + fetch; in the real page the store falls back to
// the browser globals. The first paint is local-only (offline-first): the store's
// init() populates own tabs + cache, then refresh() hits GET /api/state.
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { createStore } from "./lib/store.js";
import { formatDateTime, formatTime, planGateNotice } from "./lib/status.js";
import { confirmHeadline, impactLines } from "./lib/impact.js";
import { cleanTitle, hostOf, plural } from "./lib/bookmarks.js";

const EMPTY_DRAFT = { id: null, pattern: "", instance_id: "", singleton: false };

// Row "favicons" are CSS swatches derived from the host, NOT <img src=favIconUrl>.
// A tab's favicon url points at the SITE (or at a chrome:// favicon service), so
// painting it would make a page that promises to work offline fire a network request
// per row — hundreds of them — and leak the whole tab list to every one of those hosts
// on every newtab. A deterministic colour per host reads just as well in a dense list.
const SWATCH_COLORS = [
  "#4c6ef5",
  "#e8590c",
  "#2b8a3e",
  "#9c36b5",
  "#c2255c",
  "#d6336c",
  "#f08c00",
  "#0c8599",
  "#5b53d6",
  "#1c7ed6",
];

// NO intermediate modulus here. `% 100000` before a final `% 10` COLLAPSES the hash:
// 100000 is divisible by 10, so the truncation is invisible to the last digit, and
// 31 ≡ 1 (mod 10) turns the polynomial into a plain digit sum — every anagram of a
// host got the same colour ("a.com"/"c.moa", "habr.com"/"crab.hom"). Keep the full
// 32-bit accumulator and take the modulus ONCE, against the palette length.
function swatchColor(host) {
  const s = String(host || "");
  if (!s) return "#a5a1b2";
  let hash = 0;
  for (let i = 0; i < s.length; i += 1) hash = (Math.imul(hash, 31) + s.charCodeAt(i)) | 0;
  return SWATCH_COLORS[Math.abs(hash) % SWATCH_COLORS.length];
}

// "15:42" for a history row. Local wall clock: these stamps are the BROWSER's own,
// not the service's, so no server-offset correction applies to them.
function clockLabel(ms) {
  if (ms == null) return "";
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ONE url parse per row, done ONCE in a computed — never in the template.
//
// The template used to call `swatchColor(hostOf(t.url))`, `cleanTitle(t.title, t.url)`
// (which parses the url AGAIN inside) and `hostOf(t.url)` on every row: three `new URL`
// per row, re-run on EVERY render. And a render happens every second — store.tick()
// moves `clockTick`, which `foreignGroups`/`statusRows` read, so the
// whole component re-renders once a second whether or not anything moved.
//
// MEASURED, A/B in one process, on a 452-row profile (200 own tabs in 6 windows, 160
// foreign, 40 bookmarks, 40 history rows) by counting `new URL` constructions per tick:
//   inline template  1276 parses per tick   ≈0.5-0.7 ms of pure parsing, every second
//   row views           0 parses per tick
// (wall-clock per tick over the same rows: 10.1 -> 8.4 ms best-of-12, 16.1 -> 14.1 ms
// median; the machine was too noisy for the wall clock to be the evidence — the parse
// count is.) The old template was `{{ t.title || t.url }}` and cost nothing per row —
// this is the regression the row views pay back.
//
// The computeds below read ONLY the tab/bookmark/history data and the search string
// (never `clockTick`), so a tick re-renders from already-computed strings.
function rowView(item) {
  const host = hostOf(item.url);
  // `host` is handed to cleanTitle so the url is parsed ONCE, not twice.
  return {
    ...item,
    host,
    label: cleanTitle(item.title, item.url, host),
    swatch: swatchColor(host),
  };
}

export default {
  name: "Startpage",
  props: {
    deps: { type: Object, default: () => ({}) },
    // Tests can disable the auto init/refresh to drive the store by hand.
    autostart: { type: Boolean, default: true },
  },
  setup(props) {
    const store = createStore(props.deps);

    function onAddQuickLink(event) {
      const form = event.target;
      const url = form.elements.url.value.trim();
      const title = form.elements.title.value.trim();
      if (!url) return;
      store.addQuickLink(url, title || null);
      form.reset();
      quickAddOpen.value = false;
    }

    // --- column chrome (§10 redesign) ---------------------------------------
    // Panels that COLLAPSE do so through a CSS class, never v-if/v-show: the
    // add-a-link form and the rules editor carry data-role attributes the tests
    // address straight after mount (`[data-role="rule-form"] input[name=pattern]`),
    // so the elements must stay in the DOM whether or not the panel is open. Swap
    // either for v-if and those tests stop finding their inputs.
    const quickAddOpen = ref(false);
    const bookmarkAddOpen = ref(false);
    const rulesOpen = ref(false);
    const editingBookmarkId = ref(null);
    // A delete armed for exactly ONE bookmark, waiting for its second click — the same
    // gate the rules list uses below (`pendingDeleteId`), for the same reason: the row's
    // "×" sits one pixel from the link, and a bookmark deleted by a slip is gone from the
    // BROWSER, not just from this page. There is no undo behind it (chrome.bookmarks has
    // no trash) and the optimistic rollback only fires when chrome REFUSES — a successful
    // accidental delete is simply successful.
    const pendingBookmarkDeleteId = ref(null);
    // WHEN the row was armed (real wall clock, never the injected test clock — this
    // guard is about human reaction time, not about the service's timeline).
    const pendingBookmarkDeleteAt = ref(0);
    const renameInput = ref(null);

    function bindRenameInput(el) {
      if (el) renameInput.value = el;
    }

    async function startBookmarkRename(bookmark) {
      editingBookmarkId.value = bookmark.id;
      // Editing a row disarms a delete armed for another one (and for this one): the
      // human moved on, and an armed "×" that survives the move turns the NEXT single
      // click into a deletion. Same rule editRule() applies to the rules list.
      disarmBookmarkDelete();
      // The ✎ button is REPLACED by the input, so the focus it held is destroyed with
      // it: without moving focus here the input never gets it, @blur can therefore never
      // fire, and a human without a mouse has no way into — or out of — the edit.
      await nextTick();
      const el = renameInput.value;
      if (el && typeof el.focus === "function") {
        el.focus();
        // Select the old title: the common edit is "replace it", and having to clear the
        // field first is the difference between one keystroke and a dozen.
        if (typeof el.select === "function") el.select();
      }
    }

    // Enter, blur-with-a-change and Esc ALL end up here or in cancelBookmarkRename, and
    // several of them fire for one gesture (Enter commits, the input unmounts, and its
    // blur arrives afterwards). `editingBookmarkId` is the latch that makes the second
    // arrival a no-op — without it Enter would send two identical chrome.bookmarks
    // writes, and Esc would be overruled by the blur that follows it.
    async function commitBookmarkRename(bookmark, title) {
      if (editingBookmarkId.value !== bookmark.id) return;
      editingBookmarkId.value = null;
      const typed = String(title || "").trim();
      // AN EMPTY FIELD IS A RENAME, NOT A CANCEL. Dropping it silently (the old
      // `if (!next) return`) closed the editor and left the old name in place —
      // visually indistinguishable from Esc, so the human cannot tell whether the
      // browser refused, the click missed, or nothing was sent at all.
      //
      // Of the two ways out, this one keeps the editor's contract intact: Enter/blur
      // ALWAYS commit, Esc is the only cancel. Refusing to close on an empty field
      // would mean fighting a blur — the human clicked somewhere else, and re-focusing
      // an input they just left traps them in it with no keyboard way out.
      // So an empty title falls back to the SAME name the row would show anyway
      // (cleanTitle's empty-title answer: the host, or the raw url when there is no
      // host — file:///…, about:blank). The rename is real and visible, and the
      // browser never ends up with a nameless bookmark this page cannot label.
      const next = typed || cleanTitle("", bookmark.url);
      if (next === bookmark.title) return;
      await store.renameBookmark(bookmark, next);
    }

    function cancelBookmarkRename() {
      editingBookmarkId.value = null;
    }

    // Two clicks to delete, like the rules list: the first ARMS the row (it turns red and
    // says so), the second removes it. Clicking "×" on a different bookmark re-arms that
    // one instead of firing this one.
    //
    // …plus a DELIBERATE DEAD TIME between the two clicks. Arming here is SYNCHRONOUS:
    // without it an ordinary double-click (two events milliseconds apart, one physical
    // gesture) arms and fires in the same burst, and the bookmark is gone from the
    // BROWSER without a single frame the human could have seen — chrome.bookmarks has no
    // trash and the store's rollback only fires when chrome REFUSES the write.
    //
    // The rules list has no such guard and needs none: there the first click goes to the
    // SERVER for its impact preview (a whole-pass computation, a second or two) and the
    // gate is only armed when the 409 comes back — the network is the dead time. Here
    // nothing is awaited between the clicks, so the dead time has to be stated.
    const BOOKMARK_CONFIRM_DELAY_MS = 350;

    function disarmBookmarkDelete() {
      pendingBookmarkDeleteId.value = null;
      pendingBookmarkDeleteAt.value = 0;
    }

    async function onDeleteBookmark(bookmark) {
      const clickedAt = Date.now();
      if (pendingBookmarkDeleteId.value !== bookmark.id) {
        pendingBookmarkDeleteId.value = bookmark.id;
        pendingBookmarkDeleteAt.value = clickedAt;
        return;
      }
      // Too soon: the row STAYS armed (the human sees "подтвердить ×" and can finish
      // the delete with a deliberate second click) — the burst is swallowed, not
      // rewarded and not punished.
      if (clickedAt - pendingBookmarkDeleteAt.value < BOOKMARK_CONFIRM_DELAY_MS) return;
      disarmBookmarkDelete();
      await store.deleteBookmark(bookmark);
    }

    // Typing in the search box moves rows in and out of the list under an armed "×":
    // the armed row can be filtered away and come back STILL armed, so the next single
    // click on it deletes. The rules editor disarms on any draft edit for exactly this
    // reason (see the draftRevision watcher); the favourites column follows the same
    // discipline — searching, or opening the add-a-bookmark form, disarms.
    function onSearchInput(value) {
      disarmBookmarkDelete();
      store.setSearch(value);
    }

    function toggleBookmarkAdd() {
      disarmBookmarkDelete();
      bookmarkAddOpen.value = !bookmarkAddOpen.value;
    }

    // §10's favourites column owns the browser's bookmarks, and the manifest's write
    // permission is justified by "renames/removes/adds entries in place" — so adding has
    // to be reachable, not just implemented. Same shape as the quick-link form above.
    function onAddBookmark(event) {
      const form = event.target;
      const url = form.elements.url.value.trim();
      const title = form.elements.title.value.trim();
      if (!url) return;
      // Default parent: the first real folder chrome reports (the bookmarks bar on a
      // normal profile). `null` lets chrome choose when there is no folder at all.
      const parent = store.bookmarkFolders.value[0];
      store.addBookmark(url, title || null, parent ? parent.id : null);
      form.reset();
      bookmarkAddOpen.value = false;
    }

    // --- row views (see rowView above): the url is parsed ONCE per row ---------
    // Every one of these depends only on the lists and the search string. NONE of them
    // reads `clockTick`, so the 1 s tick re-renders them from ready strings instead of
    // re-parsing hundreds of urls.
    const quickLinkRows = computed(() => store.filteredQuickLinks.value.map(rowView));
    const bookmarkSections = computed(() =>
      store.bookmarkGroups.value.map((g) => ({ ...g, items: g.items.map(rowView) })),
    );
    const tabWindowSections = computed(() =>
      store.tabWindowGroups.value.map((w) => ({ ...w, tabs: w.tabs.map(rowView) })),
    );
    // History rows carry no swatch (the column shows a clock instead), so they skip the
    // hash — but the title still needs the host, and the url is parsed once for both.
    const historySections = computed(() =>
      store.historyGroups.value.map((d) => ({
        ...d,
        items: d.items.map((h) => ({
          ...h,
          label: cleanTitle(h.title, h.url, hostOf(h.url)),
          time: clockLabel(h.lastVisitTime),
        })),
      })),
    );
    // The foreign columns are the reason this matters most: `foreignGroups` DOES read the
    // ticking clock (it folds in the instance status), so anything computed inside it is
    // recomputed every second. The rows are therefore decorated from `foreignTabGroups`,
    // the clock-free half of that pair, and looked up by instance id at render time.
    const foreignRowsByInstance = computed(() => {
      const byId = new Map();
      for (const g of store.foreignTabGroups.value) byId.set(g.instanceId, g.tabs.map(rowView));
      return byId;
    });
    const foreignRows = (instanceId) => foreignRowsByInstance.value.get(instanceId) || [];

    // The header pill: how much of everything is on this page right now.
    const headerSummary = computed(() => {
      const tabs = store.filteredOwnTabs.value.length;
      const windows = store.tabWindowGroups.value.length;
      const instances = store.foreignGroups.value.length + 1; // own instance included
      return [
        `${tabs} ${plural(tabs, "вкладка", "вкладки", "вкладок")}`,
        `${windows} ${plural(windows, "окно", "окна", "окон")}`,
        `${instances} ${plural(instances, "инстанс", "инстанса", "инстансов")}`,
      ].join(" · ");
    });

    // --- stop status bar (§7) ----------------------------------------------
    // The stop is INDEFINITE — a server stamp, not a deadline — so nothing here
    // ticks: a plain null-check decides the row. (The four instance states still
    // ride the store's ticking serverNow; that clock just no longer feeds this row.)
    let clockTimer = null;
    const isStopped = computed(() => store.stoppedAt.value != null);
    // Date + time for the stopped row only: an indefinite stop can span days, and a
    // bare time-of-day would read as "today" however old the stop is. (The server
    // stamp is rendered as a LOCAL wall-clock time — the offset undone.)
    const serverDateTime = (ms) => formatDateTime(store.localFromServer(ms));

    // The over-threshold latch (§7 "Порог действий на проход", see status.js): the
    // last pass's plan exceeded MAX_ACTIONS_PER_PASS and waits for one confirming
    // click. Self-refreshing every pass, self-clearing when the plan shrinks below
    // the threshold; phase B keeps executing meanwhile.
    const gateNotice = computed(() =>
      store.resumePending.value ? planGateNotice(store.pendingPlan.value) : null,
    );

    // --- raise a space / run all rules now (§62 items 3 & 5) ---------------
    async function onRaiseInstance(row) {
      // Only a raisable row acts (own browser / disconnected / no window => no-op).
      if (!row.raisable) return;
      await store.raiseInstance(row.id);
    }
    async function onRunRulesNow() {
      await store.runRulesNow();
    }

    // --- открыть регистрацию нового браузера (§13) --------------------------
    async function onOpenEnrollment() {
      await store.openEnrollment();
    }
    // How long the window the last click armed stays open, in whole minutes. Derived
    // HERE and not inline in the template: the template renders text, it does not do
    // arithmetic. Floored at 1 so a window with seconds left never reads as "0 мин".
    const enrollWindowMinutes = computed(() => {
      const result = store.enrollWindow.value;
      const seconds = result && typeof result.seconds === "number" ? result.seconds : 0;
      return Math.max(1, Math.round(seconds / 60));
    });

    // --- stop gate override (§7) -------------------------------------------
    async function onForce() {
      await store.retryForced();
    }
    async function onPause() {
      // Indefinite stop — no duration to pass; only «Старт» brings it back.
      await store.pauseCurator();
    }
    async function onResume() {
      // Re-entry guard on top of the :disabled binding: DELETE /api/pause spans the
      // whole pass (tens of seconds on a big fleet), and a second activation after
      // the stop cleared server-side would arrive as a confirm of a plan the human
      // never saw. Keyboard/Enter can double-activate before Vue re-renders the
      // disabled attribute, so the handler itself must refuse too.
      if (store.resuming.value) return;
      await store.resumeCurator();
    }

    // --- rules editor local state (§8/§10) ---------------------------------
    const draft = reactive({ ...EMPTY_DRAFT });
    const draftOp = ref("create"); // "create" | "update"
    const confirmPending = ref(false); // a save came back 409 (confirm gate)
    const pendingDeleteId = ref(null); // a delete came back 409; armed for a 2nd click

    // A CONFIRMATION IS BOUND TO THE EXACT RULE THAT WAS PREVIEWED (§8). The draft is
    // v-model-bound, so any keystroke can turn the armed rule into a different one:
    // preview `borneo.lc` (2 relocations, 0 closures), get the 409, widen the pattern to
    // `corp.example` (300/120), click "Подтвердить и сохранить" — and confirm_impact
    // would go out for a rule whose impact the server never showed, with the stale 2/0
    // still on screen. That is exactly the echo-confirmation the gate exists to stop.
    //
    // `draftRevision` counts edits and is the ONLY reliable guard, because disarming on
    // edit is not enough on its own: the server's whole-pass preview takes a second or
    // two, `confirmPending` is set AFTER that await, and an edit made DURING the request
    // is disarmed by this watcher and then re-armed by the arriving 409. onSave
    // therefore snapshots the revision at send time and refuses to arm if it moved.
    //
    // `pendingDeleteId` is the same class: an armed delete for rule A must not survive
    // the human editing A (its impact was computed for the pre-edit rule).
    const draftRevision = ref(0);
    watch(
      () => [draft.pattern, draft.instance_id, draft.singleton],
      () => {
        draftRevision.value += 1;
        if (confirmPending.value || pendingDeleteId.value !== null) {
          store.rulesPreview.value = null;
        }
        confirmPending.value = false;
        pendingDeleteId.value = null;
      },
    );

    // WHICH op produced the preview currently on screen. The confirm gate has three
    // independent grounds and one of them (the empty↔non-empty boundary) is derived by
    // elimination, which only holds for create/update — a DELETE is gated
    // unconditionally, so it must not be told "this is your first rule". See impact.js.
    const previewOp = ref("create");
    const impactBlock = computed(() =>
      store.rulesPreview.value
        ? impactLines(store.rulesPreview.value, {
            op: previewOp.value,
            requiresConfirm: confirmPending.value || pendingDeleteId.value !== null,
          })
        : [],
    );
    const impactHeadline = computed(() =>
      store.rulesPreview.value ? confirmHeadline(store.rulesPreview.value, previewOp.value) : "",
    );

    function resetDraft() {
      Object.assign(draft, EMPTY_DRAFT);
      draftOp.value = "create";
      confirmPending.value = false;
      pendingDeleteId.value = null;
      store.rulesPreview.value = null;
    }

    function editRule(rule) {
      Object.assign(draft, {
        id: rule.id,
        pattern: rule.pattern,
        instance_id: rule.instance_id,
        singleton: !!rule.singleton,
      });
      draftOp.value = "update";
      confirmPending.value = false;
      // Explicit, not only via the watcher: re-clicking "Изменить" on the rule ALREADY
      // in the draft changes no field, so the watcher would not fire and an armed
      // delete would survive.
      pendingDeleteId.value = null;
      store.rulesPreview.value = null;
    }

    // Preview BEFORE save (§8) — always available so the human sees the impact first.
    async function onPreview() {
      confirmPending.value = false;
      pendingDeleteId.value = null;
      previewOp.value = draftOp.value;
      await store.previewRuleDraft(draftOp.value, draft);
    }

    async function onSave() {
      // A save is about the DRAFT, so any delete armed for a list row is stale from
      // here on (the rule set is about to change under it).
      pendingDeleteId.value = null;
      // Snapshot the draft revision BEFORE the request. The server's whole-pass preview
      // runs for a second or two — long enough for the human to widen the pattern — and
      // the 409 handler below arms the gate AFTER that await. Without this check the
      // arriving 409 re-arms a gate the edit had just cleared, and the next click sends
      // confirm_impact:true for a rule whose impact was never shown (§8).
      const sentRevision = draftRevision.value;
      previewOp.value = draftOp.value;
      const res = await store.saveRuleDraft(draftOp.value, draft, {
        confirmImpact: confirmPending.value,
      });
      if (draftRevision.value !== sentRevision) {
        // The rule changed under the request: whatever came back describes the OLD one.
        confirmPending.value = false;
        store.rulesPreview.value = null;
        return; // the next click is a fresh probe for the rule now in the form
      }
      if (res.needsConfirm) {
        confirmPending.value = true; // show the impact + a confirm button
        return;
      }
      if (res.ok) resetDraft();
    }

    async function onDelete(rule) {
      // DELETE is gated (§8): the FIRST click surfaces the impact (a 409 preview)
      // and arms this rule; only a SECOND click on the same rule confirms. Never
      // auto-confirm in one click — the human must see the impact and act again
      // (same contract as onSave; the server gate must not be echo-confirmed).
      previewOp.value = "delete";
      if (pendingDeleteId.value === rule.id) {
        const done = await store.saveRuleDraft("delete", { id: rule.id }, { confirmImpact: true });
        if (done.ok) pendingDeleteId.value = null;
        return;
      }
      const res = await store.saveRuleDraft("delete", { id: rule.id }, { confirmImpact: false });
      if (res.needsConfirm) {
        pendingDeleteId.value = rule.id; // impact now shown; a second click confirms
      } else if (res.ok) {
        pendingDeleteId.value = null; // no impact => deleted outright
      }
    }

    // This hook is ASYNC, so onUnmounted can run in the middle of it — a newtab the human
    // closes (or replaces by typing an address) while chrome.tabs/bookmarks/history are
    // still answering. Everything after an `await` therefore has to ask whether the
    // component is still there: without this flag `watchBookmarkChanges()` would run
    // AFTER `onUnmounted` had already torn the listeners down, attaching a set that
    // nothing will ever remove — the exact leak onUnmounted exists to prevent, one more
    // set per aborted mount.
    let alive = true;

    onMounted(async () => {
      // The 1s clock ticks regardless of autostart: the four instance states (§10)
      // must keep updating on a purely offline first paint. (The stop row no longer
      // needs it — an indefinite stop has nothing to count down.)
      if (typeof setInterval !== "undefined") {
        clockTimer = setInterval(() => store.tick(), 1000);
      }
      if (!props.autostart) return;
      await store.init(); // local-only first paint
      if (!alive) return;
      // The bookmark tree keeps moving while this page is open (Chrome's own UI, the
      // bookmark bar, another newtab). Subscribe AFTER the first read so the initial
      // list is never re-read for nothing. Torn down in onUnmounted below.
      store.watchBookmarkChanges();
      await store.refresh(); // background live refresh
      if (!alive) return;
      await store.loadRules(); // rules editor needs the network (§10)
    });

    onUnmounted(() => {
      alive = false;
      if (clockTimer != null) clearInterval(clockTimer);
      // Both are LEAKS if skipped: the interval keeps ticking into a dead store, and the
      // chrome.bookmarks listeners hold the store (and this component's closures) alive
      // for the lifetime of the extension, one more set per remount.
      store.unwatchBookmarkChanges();
    });

    return {
      store,
      formatTime,
      cleanTitle,
      hostOf,
      swatchColor,
      clockLabel,
      headerSummary,
      quickLinkRows,
      bookmarkSections,
      tabWindowSections,
      historySections,
      foreignRows,
      onSearchInput,
      quickAddOpen,
      bookmarkAddOpen,
      toggleBookmarkAdd,
      rulesOpen,
      editingBookmarkId,
      pendingBookmarkDeleteId,
      bindRenameInput,
      startBookmarkRename,
      commitBookmarkRename,
      cancelBookmarkRename,
      onDeleteBookmark,
      onAddBookmark,
      onAddQuickLink,
      draft,
      draftOp,
      confirmPending,
      resetDraft,
      editRule,
      onPreview,
      onSave,
      onDelete,
      pendingDeleteId,
      isStopped,
      gateNotice,
      impactBlock,
      impactHeadline,
      serverDateTime,
      onPause,
      onResume,
      onRaiseInstance,
      onRunRulesNow,
      onOpenEnrollment,
      enrollWindowMinutes,
      onForce,
    };
  },
};
</script>
<template>
  <!-- The page is a FIXED-HEIGHT board (§10): the app grid owns the viewport, each
       column scrolls on its own and the page itself never scrolls. The CSS lives
       inline in index.html (NOT in an SFC <style> block) so the built bundle stays
       pure JS — see the comment there. -->
  <div class="sp-app">
    <header class="sp-header">
      <span class="sp-title">Новая вкладка</span>
      <span class="sp-pill">
        <i class="sp-dot" :class="store.offline.value ? 'stale' : 'ok'"></i>
        <span>{{ headerSummary }}</span>
      </span>
      <span class="sp-sub sp-conn">
        <template v-if="store.offline.value">офлайн</template>
        <template v-else>на связи</template>
      </span>
    </header>

    <input
      class="sp-search"
      type="search"
      placeholder="Поиск по вкладкам, избранному и истории…"
      :value="store.search.value"
      @input="onSearchInput($event.target.value)"
    />

    <div class="sp-cols">
      <!-- COLUMN 1 — favourites: the service's own quick links (synced, offline-queued)
           followed by the browser's bookmarks (local, chrome.bookmarks). -->
      <section class="sp-card">
        <div class="sp-card-head">
          <span class="sp-card-title">Избранное</span>
          <span class="sp-card-count">
            {{ store.filteredQuickLinks.value.length + store.filteredBookmarks.value.length }}
          </span>
          <button
            class="sp-icon-btn"
            type="button"
            title="Добавить быструю ссылку"
            @click="quickAddOpen = !quickAddOpen"
          >+</button>
        </div>
        <div class="sp-col-body">
          <section class="sp-sec" data-role="quick-links">
            <div class="sp-sec-head">
              <b>Быстрые ссылки</b>
              <span class="sp-sec-count">{{ store.filteredQuickLinks.value.length }}</span>
            </div>
            <ul class="sp-list">
              <!-- `:title` carries the FULL url on every row of every column: the title
                   is cleaned (the site tail is cut) and both it and the host are
                   truncated with text-overflow, so the visible text is not always enough
                   to tell two rows apart. The native tooltip is the only place the whole
                   address is still available. -->
              <li v-for="q in quickLinkRows" :key="q.url" class="sp-item">
                <i class="sp-swatch" :style="{ background: q.swatch }"></i>
                <a class="sp-item-title" :href="q.url" :title="q.url">{{ q.label }}</a>
                <span class="sp-item-host">{{ q.host }}</span>
                <button class="sp-remove" title="Удалить" @click.prevent="store.removeQuickLink(q)">×</button>
              </li>
              <li v-if="store.filteredQuickLinks.value.length === 0" class="sp-empty">
                Нет быстрых ссылок
              </li>
            </ul>
            <!-- COLLAPSED THROUGH CSS, never v-if/v-show: every data-role on this page
                 is a contract the tests address directly after mount, so the element
                 has to be in the DOM whether the panel is open or not. -->
            <form
              class="sp-ql-add"
              :class="{ 'is-collapsed': !quickAddOpen }"
              data-role="ql-add"
              @submit.prevent="onAddQuickLink"
            >
              <input name="url" placeholder="https://…" />
              <input name="title" placeholder="Название (необязательно)" />
              <button class="sp-btn" type="submit">Добавить</button>
            </form>
          </section>

          <!-- TODO: cap / virtualise this list on a big profile, and render the folder
               HIERARCHY (nested paths + indentation) instead of one flat section per
               parent folder. Deliberately out of scope for now. -->
          <div data-role="bookmarks">
          <div class="sp-sec-head">
            <b>Закладки</b>
            <span class="sp-sec-count">{{ store.filteredBookmarks.value.length }}</span>
            <button
              class="sp-icon-btn"
              type="button"
              title="Добавить закладку"
              data-role="bookmark-add-toggle"
              @click="toggleBookmarkAdd"
            >+</button>
          </div>
          <!-- COLLAPSED THROUGH CSS, never v-if/v-show — same contract as the ql-add
               form above: the data-role node stays in the DOM whether it is open or not. -->
          <form
            class="sp-ql-add"
            :class="{ 'is-collapsed': !bookmarkAddOpen }"
            data-role="bookmark-add"
            @submit.prevent="onAddBookmark"
          >
            <input name="url" placeholder="https://…" />
            <input name="title" placeholder="Название (необязательно)" />
            <button class="sp-btn" type="submit">Добавить</button>
          </form>
          <section v-for="g in bookmarkSections" :key="g.id" class="sp-sec">
            <div class="sp-sec-head is-sticky">
              <b>{{ g.title }}</b>
              <span class="sp-sec-count">{{ g.items.length }}</span>
            </div>
            <ul class="sp-list">
              <li
                v-for="b in g.items"
                :key="b.id"
                class="sp-item"
                :class="{ 'is-armed': pendingBookmarkDeleteId === b.id }"
              >
                <template v-if="editingBookmarkId === b.id">
                  <!-- Enter saves, Esc cancels, leaving the field saves. NOT `change`:
                       it fires for the same gestures these three already handle, so it
                       only added a second commit per rename (and a second
                       chrome.bookmarks write). Several of these still fire for one
                       gesture — Enter commits and the removed input then blurs — which
                       the commit/cancel latch absorbs (see App's script). -->
                  <input
                    :ref="bindRenameInput"
                    class="sp-inline-input"
                    name="bookmark-title"
                    data-role="bookmark-rename"
                    :value="b.title"
                    @keyup.enter="commitBookmarkRename(b, $event.target.value)"
                    @blur="commitBookmarkRename(b, $event.target.value)"
                    @keyup.esc="cancelBookmarkRename()"
                  />
                </template>
                <template v-else>
                  <i class="sp-swatch" :style="{ background: b.swatch }"></i>
                  <a class="sp-item-title" :href="b.url" :title="b.url">{{ b.label }}</a>
                  <span class="sp-item-host">{{ b.host }}</span>
                  <span class="sp-item-actions">
                    <button
                      class="sp-icon-btn"
                      type="button"
                      title="Переименовать"
                      @click.prevent="startBookmarkRename(b)"
                    >✎</button>
                    <!-- Two-click gate, exactly like the rules list: the first click arms
                         THIS row and says so, the second one deletes. -->
                    <button
                      class="sp-remove"
                      :class="{ 'is-confirm': pendingBookmarkDeleteId === b.id }"
                      :title="pendingBookmarkDeleteId === b.id ? 'Подтвердите удаление закладки' : 'Удалить'"
                      @click.prevent="onDeleteBookmark(b)"
                    >{{ pendingBookmarkDeleteId === b.id ? 'подтвердить ×' : '×' }}</button>
                  </span>
                </template>
              </li>
            </ul>
          </section>
          <!-- The same empty state the other three lists have. Without it a profile with
               no bookmarks — or a search that filtered them all out — showed the section
               header and then nothing, which reads as "it did not load" rather than as
               "there is nothing here". -->
          <div v-if="store.filteredBookmarks.value.length === 0" class="sp-empty">
            Нет закладок
          </div>
          </div>
        </div>
      </section>

      <!-- COLUMN 2 — tabs. NEVER `column-count` / any multi-column flow in here: this
           column scrolls VERTICALLY, and a multi-column flow continues SIDEWAYS, so
           the reading order stops matching the scroll order and everything past the
           last visible column is pushed outside the box. Measured on a real 177-tab /
           11-window profile: an entire instance vanished off the right edge. A plain
           vertical list of window sections is the only layout that survives. -->
      <section class="sp-card">
        <div class="sp-card-head">
          <span class="sp-card-title">Вкладки</span>
          <span class="sp-card-count">{{ store.filteredOwnTabs.value.length }}</span>
        </div>
        <div class="sp-col-body">
          <div data-role="own-tabs">
            <section
              v-for="(w, i) in tabWindowSections"
              :key="w.windowId == null ? 'w' + i : w.windowId"
              class="sp-sec"
            >
              <!-- Click the header to RAISE this own window (§62 item 4) without changing
                   the active tab inside it (contrast the per-tab jumpOwn below, which
                   activates a tab and closes the newtab). A null windowId (a search-only
                   pseudo-group) simply no-ops in raiseOwnWindow. -->
              <div
                class="sp-sec-head is-sticky is-raisable"
                data-role="own-window-head"
                @click="store.raiseOwnWindow(w.windowId)"
              >
                <i class="sp-dot ok"></i>
                <!-- `w.ordinal`, NOT the index in this v-for: the list is FILTERED by the
                     search box, so numbering by position renumbered the windows on every
                     keystroke. The ordinal is computed once over the unfiltered tabs
                     (store.tabWindowGroups). "Этот браузер" is not repeated here — the
                     card this section lives in is already the own-browser card, and the
                     first window is not more "this browser" than the others. -->
                <b>Окно {{ w.ordinal }}</b>
                <span class="sp-sec-sub">{{ w.label }}</span>
                <span class="sp-sec-count">{{ w.count }}</span>
              </div>
              <ul class="sp-list">
                <li v-for="t in w.tabs" :key="t.tab_id" class="sp-item" :title="t.url" @click="store.jumpOwn(t)">
                  <i class="sp-swatch" :style="{ background: t.swatch }"></i>
                  <span class="sp-item-title">{{ t.label }}</span>
                  <span class="sp-item-host">{{ t.host }}</span>
                </li>
              </ul>
            </section>
            <div v-if="store.filteredOwnTabs.value.length === 0" class="sp-empty">Нет вкладок</div>
          </div>

          <!-- Foreign instances: one section per instance, same row shape. Offline the
               rows are inactive and the header carries "кэш от <время>" (§10). -->
          <section
            v-for="g in store.foreignGroups.value"
            :key="g.instanceId"
            class="sp-sec"
            data-role="foreign-group"
          >
            <div class="sp-sec-head is-sticky">
              <i class="sp-dot" :class="g.status.state"></i>
              <b>{{ g.title }}</b>
              <span v-if="!g.jumpable" class="sp-sec-sub">
                кэш от {{ formatTime(store.cachedAt.value) }}
              </span>
              <span v-else class="sp-sec-sub">{{ g.status.label }}</span>
              <span class="sp-sec-count">{{ g.tabs.length }}</span>
            </div>
            <!-- The ROWS come from foreignRows(), not from `g.tabs`: `g` is rebuilt every
                 second (it carries the clock-dependent status), while foreignRows() is
                 keyed off the clock-free grouping, so the decorated rows survive a tick
                 untouched — see the row-view computeds in App's script. -->
            <ul class="sp-list">
              <li
                v-for="t in foreignRows(g.instanceId)"
                :key="t.tab_id"
                class="sp-item"
                :class="{ 'is-inactive': !g.jumpable }"
                :title="t.url"
                @click="g.jumpable && store.jumpForeign(g.instanceId, t)"
              >
                <i class="sp-swatch" :style="{ background: t.swatch }"></i>
                <span class="sp-item-title">{{ t.label }}</span>
                <span class="sp-item-host">{{ t.host }}</span>
              </li>
            </ul>
          </section>
        </div>
      </section>

      <!-- COLUMN 3 — history, grouped by local calendar day (chrome.history, local). -->
      <section class="sp-card">
        <div class="sp-card-head">
          <span class="sp-card-title">История</span>
          <span class="sp-card-count">{{ store.filteredHistory.value.length }}</span>
        </div>
        <div class="sp-col-body" data-role="history">
          <section v-for="d in historySections" :key="d.key" class="sp-sec">
            <div class="sp-sec-head is-sticky">
              <b>{{ d.label }}</b>
              <span class="sp-sec-count">{{ d.items.length }}</span>
            </div>
            <ul class="sp-list">
              <li v-for="(h, i) in d.items" :key="h.url + '#' + i" class="sp-item">
                <span class="sp-time">{{ h.time }}</span>
                <a class="sp-item-title" :href="h.url" :title="h.url">{{ h.label }}</a>
              </li>
            </ul>
          </section>
          <div v-if="store.filteredHistory.value.length === 0" class="sp-empty">История пуста</div>
        </div>
      </section>
    </div>

    <!-- Status bar: enroll banner (§7) + stop/start ROW (§7) + four instance
         states (§10), laid out as one wrapping strip of chips. -->
    <footer class="sp-status" data-role="status-bar">
      <!-- Enroll banner (§7): shows "адрес не настроен" / "не зарегистрирован" /
           "отозван" from getConnectionState (durable facts) — states /api/state cannot
           express. Absent when approved (the normal status rows speak). acc 13. -->
      <div
        v-if="store.enrollStatus.value"
        class="sp-status-row sp-enroll-row"
        data-role="enroll-banner"
        :data-state="store.enrollStatus.value.state"
      >
        <span class="sp-dot" :class="store.enrollStatus.value.state"></span>
        <span class="sp-status-name">Регистрация</span>
        <span class="sp-sub">— {{ store.enrollStatus.value.label }}</span>
      </div>
      <!-- Stop/start row (§7 "видимость обязательна"): a persistent ROW, NOT a
           badge/toast. Renders from cache too (offline-first). The stop is
           INDEFINITE — a "since" stamp, never a countdown. -->
      <div class="sp-status-row sp-pause-row" data-role="pause-row">
        <template v-if="isStopped">
          <span class="sp-dot paused"></span>
          <span class="sp-status-name">Остановлено</span>
          <!-- Date + time, not time-of-day: the stop is indefinite and can span days. -->
          <span class="sp-sub" data-role="pause-since">— с {{ serverDateTime(store.stoppedAt.value) }}</span>
          <!-- A latch armed UNDER the stop: «Старт» resumes through the NORMAL gate
               and does NOT confirm this plan, so it must be visible here — the
               informed confirm is the NEXT click, after the start re-shows it. -->
          <span
            v-if="store.pendingPlan.value"
            class="sp-sub sp-pending-plan"
            data-role="pending-plan"
          >Переселений {{ store.pendingPlan.value.relocations }},
            закрытий {{ store.pendingPlan.value.closures }}<template
              v-if="store.pendingPlan.value.deferred"
            >, отложено {{ store.pendingPlan.value.deferred }}</template>
            — ждёт подтверждения после старта</span>
          <button
            class="sp-btn"
            type="button"
            data-role="pause-resume"
            :disabled="store.offline.value || store.resuming.value"
            @click="onResume"
          >Старт</button>
        </template>
        <!-- The over-threshold latch (§7 "Порог действий на проход"): the plan
             exceeded MAX_ACTIONS_PER_PASS and waits for ONE confirming click.
             Self-refreshing every pass, self-clearing below the threshold; phase B
             keeps executing — so this is a gate on the salvo, not a stop. The human
             confirms SEEING the counts, never a bare boolean. -->
        <template v-else-if="gateNotice">
          <span class="sp-dot paused"></span>
          <span class="sp-status-name">{{ gateNotice.title }}</span>
          <span class="sp-sub" data-role="pause-pending">— {{ gateNotice.sub }}</span>
          <span
            v-if="store.pendingPlan.value"
            class="sp-sub sp-pending-plan"
            data-role="pending-plan"
          >Переселений {{ store.pendingPlan.value.relocations }},
            закрытий {{ store.pendingPlan.value.closures }}<template
              v-if="store.pendingPlan.value.deferred"
            >, отложено {{ store.pendingPlan.value.deferred }}</template></span>
          <button
            class="sp-btn"
            type="button"
            data-role="pause-confirm"
            :disabled="store.offline.value || store.resuming.value"
            @click="onResume"
          >Выполнить</button>
        </template>
        <template v-else>
          <span class="sp-dot ok"></span>
          <span class="sp-status-name">Автоматика активна</span>
          <button
            class="sp-btn"
            type="button"
            data-role="pause-start"
            :disabled="store.offline.value"
            @click="onPause"
          >Стоп</button>
        </template>
        <span v-if="store.pauseError.value" class="sp-sub sp-pause-error">{{ store.pauseError.value }}</span>
      </div>

      <!-- Run all rules NOW (§62 item 5): one click runs a curator pass that executes
           even an over-threshold plan (run_all bypasses the MAX_ACTIONS_PER_PASS latch
           server-side), no second confirm needed. Disabled offline. -->
      <div class="sp-status-row" data-role="run-now-row">
        <span class="sp-dot ok"></span>
        <span class="sp-status-name">Правила</span>
        <button
          class="sp-btn"
          type="button"
          data-role="run-rules-now"
          :disabled="store.offline.value"
          @click="onRunRulesNow"
        >Выполнить все правила сейчас</button>
        <span v-if="store.runNowResult.value" class="sp-sub" data-role="run-now-result">
          <template v-if="store.runNowResult.value.error">— не удалось: {{ store.runNowResult.value.error }}</template>
          <template v-else-if="store.runNowResult.value.status === 'ok'">— проход выполнен: {{ store.runNowResult.value.status }}</template>
          <template v-else>— проход не выполнен: {{ store.runNowResult.value.status }}</template>
        </span>
      </div>

      <!-- Регистрация нового браузера (§13). The page arms the window ITSELF instead of
           just linking to the console: the code has to reach the clipboard inside THIS
           document's user activation, and a tab that /admin opened carries none — the
           click would open a console and copy nothing. The code is therefore printed
           right here as the clipboard's fallback: a refused write (plain http, a lapsed
           activation) must still leave the human able to read what to type. The Админка
           link is the way to everything else the console does; it is absent when no
           service address is configured, rather than pointing nowhere. -->
      <div class="sp-status-row" data-role="enroll-window-row">
        <span class="sp-dot ok"></span>
        <span class="sp-status-name">Новый браузер</span>
        <button
          class="sp-btn"
          type="button"
          data-role="open-enrollment"
          :disabled="store.offline.value"
          @click="onOpenEnrollment"
        >Открыть регистрацию и скопировать код</button>
        <a
          v-if="store.adminUrl.value"
          class="sp-btn sp-btn-ghost"
          data-role="admin-link"
          :href="store.adminUrl.value"
          target="_blank"
          rel="noopener noreferrer"
        >Админка</a>
        <span v-if="store.enrollWindow.value" class="sp-sub" data-role="enroll-window-result">
          <template v-if="store.enrollWindow.value.error">— не удалось: {{ store.enrollWindow.value.error }}</template>
          <template v-else>— код {{ store.enrollWindow.value.code }}, окно открыто на {{ enrollWindowMinutes }} мин<template
            v-if="store.enrollWindow.value.copied"
          >, скопирован</template><template
            v-else
          > — скопировать не удалось, выделите код и скопируйте вручную</template></template>
        </span>
      </div>

      <!-- Click a space (§62 item 3): raise that instance's browser to the foreground,
           creating no tabs. A row is clickable only when raisable (foreign, connected,
           known focused window); own/disconnected rows are inert. -->
      <div
        v-for="row in store.statusRows.value"
        :key="row.id"
        class="sp-status-row"
        :class="{ 'is-raisable': row.raisable && !store.offline.value }"
        :data-role="'space-row'"
        :data-instance="row.id"
        @click="onRaiseInstance(row)"
      >
        <span class="sp-dot" :class="row.status.state"></span>
        <span class="sp-status-name">{{ row.title }}</span>
        <span class="sp-sub">— {{ row.status.label }}</span>
      </div>
      <div v-if="store.statusRows.value.length === 0" class="sp-empty">Других инстансов пока нет</div>

      <p v-if="store.fallbackMessage.value" class="sp-fallback">{{ store.fallbackMessage.value }}</p>

      <!-- Stop gate (§7): a 423 is the emergency stop doing its job, not a breakage.
           Name the reason and offer the human's own override ({force:true}) instead of
           the useless "переключитесь вручную". -->
      <p v-if="store.pauseBlock.value" class="sp-fallback sp-paused-block" data-role="pause-block">
        Куратор остановлен — действие не выполнено.
        <button class="sp-btn" type="button" data-role="pause-force" @click="onForce">
          Выполнить всё равно
        </button>
      </p>

      <button class="sp-btn sp-btn-ghost sp-push" type="button" @click="rulesOpen = !rulesOpen">
        Правила {{ store.rules.value.length }}
      </button>
    </footer>

    <!-- Rules editor (§8/§10): list + invalid highlight + preview-before-save.
         COLLAPSED THROUGH CSS ONLY (`.is-collapsed { display: none }`), never
         v-if/v-show. rules.test.js reaches into `[data-role="rule-form"]` and sets
         `input[name="pattern"]` straight after mount, with the panel closed — the
         nodes must exist in the DOM. Do not "clean this up" into a v-if. -->
    <section
      class="sp-rules"
      :class="{ 'is-collapsed': !rulesOpen }"
      data-role="rules-editor"
    >
      <div class="sp-sec-head">
        <b>Правила</b>
        <span v-if="store.rulesOffline.value" class="sp-sec-sub">офлайн — редактор недоступен</span>
        <span class="sp-sec-count">{{ store.rules.value.length }}</span>
      </div>

      <ul class="sp-list" data-role="rules-list">
        <li
          v-for="r in store.rules.value"
          :key="r.id"
          class="sp-item sp-rule"
          :class="{ 'is-invalid': r.invalid }"
          :data-invalid="r.invalid ? '1' : '0'"
        >
          <span class="sp-item-title">{{ r.pattern }} → {{ r.instance_id }}</span>
          <span v-if="r.singleton" class="sp-rule-flag">singleton</span>
          <span v-if="r.invalid" class="sp-rule-flag sp-rule-invalid" title="Правило невалидно">невалидно</span>
          <button class="sp-btn sp-btn-ghost" type="button" @click="editRule(r)">Изменить</button>
          <button
            class="sp-remove"
            :class="{ 'is-confirm': pendingDeleteId === r.id }"
            :title="pendingDeleteId === r.id ? 'Подтвердите удаление (см. влияние ниже)' : 'Удалить'"
            @click.prevent="onDelete(r)"
          >{{ pendingDeleteId === r.id ? 'подтвердить ×' : '×' }}</button>
        </li>
        <li v-if="store.rules.value.length === 0 && !store.rulesOffline.value" class="sp-empty">
          Нет правил
        </li>
      </ul>

      <form
        v-if="!store.rulesOffline.value"
        class="sp-rule-form"
        data-role="rule-form"
        @submit.prevent="onSave"
      >
        <input v-model="draft.pattern" name="pattern" placeholder="example.com[:port]" />
        <input v-model="draft.instance_id" name="instance_id" placeholder="инстанс" />
        <label class="sp-rule-singleton">
          <input v-model="draft.singleton" type="checkbox" /> singleton
        </label>
        <button class="sp-btn sp-btn-ghost" type="button" data-role="rule-preview" @click="onPreview">
          Показать влияние
        </button>
        <button class="sp-btn" type="submit" data-role="rule-save">
          {{ confirmPending ? "Подтвердить и сохранить" : "Сохранить" }}
        </button>
        <button v-if="draftOp === 'update'" class="sp-btn sp-btn-ghost" type="button" @click="resetDraft">
          Отмена
        </button>
      </form>

      <!-- Impact preview (§8): shown BEFORE the change is committed. The numbers alone
           are not the answer — the gate fires on three grounds and two of them are not
           countable, so the block names the one that applies (impact.js). -->
      <div
        v-if="store.rulesPreview.value"
        class="sp-rule-preview"
        data-role="rule-preview-out"
        :class="{ 'is-confirm': confirmPending }"
      >
        <p
          v-for="(line, i) in impactBlock"
          :key="i"
          class="sp-rule-preview-line"
        >{{ line }}</p>
        <p
          v-if="confirmPending"
          class="sp-rule-preview-line sp-rule-preview-ask"
          data-role="rule-preview-ask"
        >— {{ impactHeadline }}</p>
      </div>
      <p v-if="store.rulesError.value && !store.rulesOffline.value" class="sp-fallback">
        {{ store.rulesError.value }}
      </p>
    </section>
  </div>
</template>
