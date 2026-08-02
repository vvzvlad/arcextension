<script>
// The startpage root (§10). SFC => precompiled render function at build time (no
// runtime compiler, no string template) — the whole point of the plugin-vue setup.
//
// Rendering is driven by the store (src/lib/store.js). `deps` is an optional prop so
// tests can inject a fake chrome + fetch; in the real page the store falls back to
// the browser globals. The first paint is local-only (offline-first): the store's
// init() populates own tabs + cache, then refresh() hits GET /api/state.
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
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
    // A live local clock drives the countdown; the deadline itself comes from the
    // store (paused_until, offline-first from the cache). isPaused compares against
    // this ticking `nowTick` so the row flips to "active" the instant it elapses.
    const nowTick = ref(Date.now());
    let pauseTimer = null;
    const isPaused = computed(
      () => store.pausedUntil.value != null && store.pausedUntil.value > nowTick.value,
    );
    const pauseRemaining = computed(() =>
      isPaused.value ? formatCountdown(store.pausedUntil.value - nowTick.value) : "00:00",
    );
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

    function resetDraft() {
      Object.assign(draft, EMPTY_DRAFT);
      draftOp.value = "create";
      confirmPending.value = false;
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
      store.rulesPreview.value = null;
    }

    // Preview BEFORE save (§8) — always available so the human sees the impact first.
    async function onPreview() {
      confirmPending.value = false;
      await store.previewRuleDraft(draftOp.value, draft);
    }

    async function onSave() {
      const res = await store.saveRuleDraft(draftOp.value, draft, {
        confirmImpact: confirmPending.value,
      });
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
      // The 1s pause countdown ticks regardless of autostart (a cached pause must
      // still count down on a purely offline first paint, §7).
      if (typeof setInterval !== "undefined") {
        pauseTimer = setInterval(() => {
          nowTick.value = Date.now();
        }, 1000);
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
      onPause,
      onResume,
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

    <!-- Status bar: pause countdown ROW (§7) + four instance states (§10) -->
    <footer class="sp-status" data-role="status-bar">
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
      </div>
      <div v-if="store.statusRows.value.length === 0" class="sp-empty">Других инстансов пока нет</div>
    </footer>
  </div>
</template>
