<script>
// The startpage root (§10). SFC => precompiled render function at build time (no
// runtime compiler, no string template) — the whole point of the plugin-vue setup.
//
// Rendering is driven by the store (src/lib/store.js). `deps` is an optional prop so
// tests can inject a fake chrome + fetch; in the real page the store falls back to
// the browser globals. The first paint is local-only (offline-first): the store's
// init() populates own tabs + cache, then refresh() hits GET /api/state.
import { onMounted } from "vue";
import { createStore } from "./lib/store.js";
import { formatTime } from "./lib/status.js";

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

    onMounted(async () => {
      if (!props.autostart) return;
      await store.init(); // local-only first paint
      await store.refresh(); // background live refresh
    });

    return { store, formatTime, onAddQuickLink };
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

    <!-- Status bar: four instance states (§10) -->
    <footer class="sp-status" data-role="status-bar">
      <div v-for="row in store.statusRows.value" :key="row.id" class="sp-status-row">
        <span class="sp-dot" :class="row.status.state"></span>
        <span class="sp-status-name">{{ row.title }}</span>
        <span class="sp-sub">— {{ row.status.label }}</span>
      </div>
      <div v-if="store.statusRows.value.length === 0" class="sp-empty">Других инстансов пока нет</div>
    </footer>
  </div>
</template>
