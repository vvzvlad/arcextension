<script>
// The startpage root (§10). SFC => precompiled render function at build time (no
// runtime compiler, no string template) — the whole point of the plugin-vue setup.
//
// Rendering is driven by the store (src/lib/store.js). `deps` is an optional prop so
// tests can inject a fake chrome + fetch; in the real page the store falls back to
// the browser globals. The first paint is local-only (offline-first): the store's
// init() populates own tabs + cache, then refresh() hits GET /api/state.
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { createStore } from "./lib/store.js";
import { formatCountdown, formatTime } from "./lib/status.js";

const EMPTY_DRAFT = { id: null, pattern: "", instance_id: "", singleton: false };

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
    }

    // --- pause status bar (§7) ---------------------------------------------
    // One ticking clock drives EVERYTHING time-dependent on this page: the pause
    // countdown AND the four instance states (§10). It lives in the store
    // (store.tick/serverNow) so the comparisons happen in the SERVER scale —
    // `paused_until` and `snapshot_at` are server stamps, and a couple of seconds of
    // laptop drift would otherwise mis-render both. A computed that read a plain
    // Date.now() would also never re-evaluate: the labels would freeze at first paint.
    let pauseTimer = null;
    const isPaused = computed(
      () => store.pausedUntil.value != null && store.pausedUntil.value > store.serverNow(),
    );
    const pauseRemaining = computed(() =>
      isPaused.value ? formatCountdown(store.pausedUntil.value - store.serverNow()) : "00:00",
    );
    // A server deadline rendered as a LOCAL wall-clock time (the offset undone).
    const serverTime = (ms) => formatTime(store.localFromServer(ms));

    // --- merge windows now (§9) --------------------------------------------
    async function onMergeWindows(instanceId) {
      await store.mergeWindowsNow(instanceId);
    }

    // --- pause gate override (§7) ------------------------------------------
    async function onForce() {
      await store.retryForced();
    }
    async function onPause() {
      // Post an explicit 60 so the "Пауза на час" label is always truthful,
      // independent of the server's PAUSE_DEFAULT_MIN.
      await store.pauseCurator(60);
    }
    async function onResume() {
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

    onMounted(async () => {
      // The 1s clock ticks regardless of autostart (a cached pause must still count
      // down on a purely offline first paint, §7).
      if (typeof setInterval !== "undefined") {
        pauseTimer = setInterval(() => store.tick(), 1000);
      }
      if (!props.autostart) return;
      await store.init(); // local-only first paint
      await store.refresh(); // background live refresh
      await store.loadRules(); // rules editor needs the network (§10)
    });

    onUnmounted(() => {
      if (pauseTimer != null) clearInterval(pauseTimer);
    });

    return {
      store,
      formatTime,
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
      isPaused,
      pauseRemaining,
      serverTime,
      onPause,
      onResume,
      onMergeWindows,
      onForce,
    };
  },
};
</script>

<template>
  <div>
    <header class="sp-header">
      <span class="sp-title">Новая вкладка</span>
      <span class="sp-sub">
        <template v-if="store.offline.value">офлайн</template>
        <template v-else>на связи</template>
      </span>
    </header>

    <input
      class="sp-search"
      type="search"
      placeholder="Поиск по вкладкам и ссылкам…"
      :value="store.search.value"
      @input="store.setSearch($event.target.value)"
    />

    <!-- Quick links -->
    <section class="sp-group" data-role="quick-links">
      <div class="sp-group-head"><span class="sp-group-name">Быстрые ссылки</span></div>
      <ul class="sp-list">
        <li v-for="q in store.filteredQuickLinks.value" :key="q.url" class="sp-item">
          <a class="sp-item-title" :href="q.url">{{ q.title || q.url }}</a>
          <span class="sp-item-url">{{ q.url }}</span>
          <button class="sp-remove" title="Удалить" @click.prevent="store.removeQuickLink(q)">×</button>
        </li>
        <li v-if="store.filteredQuickLinks.value.length === 0" class="sp-empty">Нет быстрых ссылок</li>
      </ul>
      <form class="sp-ql-add" data-role="ql-add" @submit.prevent="onAddQuickLink">
        <input name="url" placeholder="https://…" />
        <input name="title" placeholder="Название (необязательно)" />
        <button class="sp-btn" type="submit">Добавить</button>
      </form>
    </section>

    <!-- Own tabs -->
    <section class="sp-group" data-role="own-tabs">
      <div class="sp-group-head"><span class="sp-group-name">Этот браузер</span></div>
      <ul class="sp-list">
        <li
          v-for="t in store.filteredOwnTabs.value"
          :key="t.tab_id"
          class="sp-item"
          @click="store.jumpOwn(t)"
        >
          <img v-if="t.fav_icon_url" class="sp-fav" :src="t.fav_icon_url" alt="" />
          <span class="sp-item-title">{{ t.title || t.url }}</span>
          <span class="sp-item-url">{{ t.url }}</span>
        </li>
        <li v-if="store.filteredOwnTabs.value.length === 0" class="sp-empty">Нет вкладок</li>
      </ul>
    </section>

    <!-- Foreign instances -->
    <section
      v-for="g in store.foreignGroups.value"
      :key="g.instanceId"
      class="sp-group"
      data-role="foreign-group"
    >
      <div class="sp-group-head">
        <span class="sp-group-name">{{ g.title }}</span>
        <span v-if="!g.jumpable" class="sp-cache-note">кэш от {{ formatTime(store.cachedAt.value) }}</span>
      </div>
      <ul class="sp-list">
        <li
          v-for="t in g.tabs"
          :key="t.tab_id"
          class="sp-item"
          :class="{ 'is-inactive': !g.jumpable }"
          @click="g.jumpable && store.jumpForeign(g.instanceId, t)"
        >
          <img v-if="t.fav_icon_url" class="sp-fav" :src="t.fav_icon_url" alt="" />
          <span class="sp-item-title">{{ t.title || t.url }}</span>
          <span class="sp-item-url">{{ t.url }}</span>
        </li>
      </ul>
    </section>

    <p v-if="store.fallbackMessage.value" class="sp-fallback">{{ store.fallbackMessage.value }}</p>

    <!-- Pause gate (§7): a 423 is the emergency stop doing its job, not a breakage.
         Name the reason and offer the human's own override ({force:true}) instead of
         the useless "переключитесь вручную". -->
    <p v-if="store.pauseBlock.value" class="sp-fallback sp-paused-block" data-role="pause-block">
      Куратор на паузе<template v-if="store.pauseBlock.value.until">
        до {{ serverTime(store.pauseBlock.value.until) }}</template>
      — действие не выполнено.
      <button class="sp-btn" type="button" data-role="pause-force" @click="onForce">
        Выполнить всё равно
      </button>
    </p>

    <!-- Rules editor (§8/§10): list + invalid highlight + preview-before-save -->
    <section class="sp-group" data-role="rules-editor">
      <div class="sp-group-head">
        <span class="sp-group-name">Правила</span>
        <span v-if="store.rulesOffline.value" class="sp-cache-note">офлайн — редактор недоступен</span>
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
          <button class="sp-btn" type="button" @click="editRule(r)">Изменить</button>
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
        <button class="sp-btn" type="button" data-role="rule-preview" @click="onPreview">
          Показать влияние
        </button>
        <button class="sp-btn" type="submit" data-role="rule-save">
          {{ confirmPending ? "Подтвердить и сохранить" : "Сохранить" }}
        </button>
        <button v-if="draftOp === 'update'" class="sp-btn" type="button" @click="resetDraft">
          Отмена
        </button>
      </form>

      <!-- Impact preview (§8): shown BEFORE the change is committed. -->
      <p
        v-if="store.rulesPreview.value"
        class="sp-rule-preview"
        data-role="rule-preview-out"
        :class="{ 'is-confirm': confirmPending }"
      >
        Переселений: {{ store.rulesPreview.value.relocations }},
        закрытий: {{ store.rulesPreview.value.closures }}
        <template v-if="confirmPending"> — требуется подтверждение</template>
      </p>
      <p v-if="store.rulesError.value && !store.rulesOffline.value" class="sp-fallback">
        {{ store.rulesError.value }}
      </p>
    </section>

    <!-- Status bar: enroll banner (§7) + pause countdown ROW (§7) + four instance states (§10) -->
    <footer class="sp-status" data-role="status-bar">
      <!-- Enroll banner (§7): shows "адрес не настроен" / "ожидает одобрения" /
           "отозён" from getConnectionState (durable facts) — states /api/state cannot
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
      <!-- Pause row (§7 "видимость обязательна"): a persistent ROW with a live
           countdown, NOT a badge/toast. Renders from cache too (offline-first). -->
      <div class="sp-status-row sp-pause-row" data-role="pause-row">
        <template v-if="isPaused">
          <span class="sp-dot paused"></span>
          <span class="sp-status-name">Пауза</span>
          <span class="sp-sub" data-role="pause-countdown">— осталось {{ pauseRemaining }}</span>
          <button
            class="sp-btn"
            type="button"
            data-role="pause-resume"
            :disabled="store.offline.value"
            @click="onResume"
          >Возобновить</button>
        </template>
        <!-- Click-wait (§7): the hour elapsed but the curator DEFERS until confirmed,
             so it is neither running nor over — show that explicitly (a forgotten pause
             in this state must not read "active"). Resume clears the latch and runs a
             pass now. -->
        <template v-else-if="store.resumePending.value">
          <span class="sp-dot paused"></span>
          <span class="sp-status-name">Пауза истекла</span>
          <span class="sp-sub" data-role="pause-pending">— ожидание подтверждения</span>
          <!-- §7: "план выводится в статус-полосу" — the human confirms the salvo
               SEEING what it will do. A bare boolean asks for a blind click on the
               largest batch the system ever runs. -->
          <span
            v-if="store.pendingPlan.value"
            class="sp-sub sp-pending-plan"
            data-role="pending-plan"
          >— будет сделано: переселений {{ store.pendingPlan.value.relocations }},
            довершений {{ store.pendingPlan.value.phaseBCompletions }},
            закрытий {{ store.pendingPlan.value.closures }}<template
              v-if="store.pendingPlan.value.deferred"
            >, отложено {{ store.pendingPlan.value.deferred }}</template></span>
          <button
            class="sp-btn"
            type="button"
            data-role="pause-confirm"
            :disabled="store.offline.value"
            @click="onResume"
          >Запустить сейчас</button>
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
          >Пауза на час</button>
        </template>
        <span v-if="store.pauseError.value" class="sp-sub sp-pause-error">{{ store.pauseError.value }}</span>
      </div>

      <div v-for="row in store.statusRows.value" :key="row.id" class="sp-status-row">
        <span class="sp-dot" :class="row.status.state"></span>
        <span class="sp-status-name">{{ row.title }}</span>
        <span class="sp-sub">— {{ row.status.label }}</span>
        <!-- §9 promises this button explicitly: the pass folds windows only after an
             hour of idleness, and "ждать час не хочется" is a real case. -->
        <button
          class="sp-btn"
          type="button"
          data-role="merge-windows"
          :data-instance="row.id"
          :disabled="store.offline.value"
          @click="onMergeWindows(row.id)"
        >Слить окна</button>
        <span
          v-if="store.mergeResult.value && store.mergeResult.value.instanceId === row.id"
          class="sp-sub"
          data-role="merge-result"
        >
          <!-- retryable (§9/§10): busy_dragging or a stale window picture — nothing
               broke, so it must not read as a failure. -->
          <template v-if="store.mergeResult.value.retryable">— {{ store.mergeResult.value.retryable }}</template>
          <template v-else-if="store.mergeResult.value.error">— слияние не удалось: {{ store.mergeResult.value.error }}</template>
          <template v-else>— слито вкладок: {{ store.mergeResult.value.merged }}</template>
        </span>
      </div>
      <div v-if="store.statusRows.value.length === 0" class="sp-empty">Других инстансов пока нет</div>
    </footer>
  </div>
</template>
