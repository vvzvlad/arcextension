import { describe, it, expect } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

import App from "../src/App.vue";
import { makeChrome, makeFetch } from "./mocks.js";

// SFC render smoke: the precompiled component renders a NON-EMPTY root from LOCAL
// sources even with no cache and no network (offline-first, §10). This is the
// component-level counterpart to the built-dist smoke test in build-gates.test.js.
describe("App renders non-empty offline-first (§10)", () => {
  it("shows own tabs, the search box and the status bar with no network", async () => {
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 1, url: "https://own/a", title: "Own A" }],
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({ state: undefined }); // offline

    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises(); // let onMounted init + refresh settle

    // Root is NON-EMPTY.
    expect(wrapper.html().trim().length).toBeGreaterThan(0);
    // The own tab painted from chrome.tabs.query.
    expect(wrapper.find('[data-role="own-tabs"]').text()).toContain("Own A");
    // The always-present scaffolding (search + status bar) is there.
    expect(wrapper.find("input.sp-search").exists()).toBe(true);
    expect(wrapper.find('[data-role="status-bar"]').exists()).toBe(true);
    // The pause ROW renders offline-first (§7): not paused → "active" + a Pause button.
    expect(wrapper.find('[data-role="pause-row"]').exists()).toBe(true);
    expect(wrapper.find('[data-role="pause-start"]').exists()).toBe(true);
    // Offline indicator is shown.
    expect(wrapper.find(".sp-header").text()).toContain("офлайн");
  });

  it("shows the click-wait state when the pause expired but the pass DEFERS (§7)", async () => {
    // resume_pending: the hour is over (paused_until null/past) but the curator waits
    // for a confirm — it must NOT read "Автоматика активна" (a forgotten pause here is
    // otherwise invisible). Drop the resume_pending branch and this reddens.
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 1, url: "https://own/a", title: "Own A" }],
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({
      state: { instances: [], tabs: [], paused_until: null, resume_pending: true },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    expect(wrapper.find('[data-role="pause-pending"]').exists()).toBe(true);
    expect(wrapper.find('[data-role="pause-confirm"]').exists()).toBe(true);
    const rowText = wrapper.find('[data-role="pause-row"]').text();
    expect(rowText).toContain("ожидание подтверждения");
    expect(rowText).not.toContain("Автоматика активна");
  });

  it("shows the DEFERRED PASS PLAN next to the confirm button (§7)", async () => {
    // §7: the plan "выводится в статус-полосу" — the human confirms the largest salvo
    // the system ever fires SEEING what it will do. A bare resume_pending boolean asks
    // for a blind click. Drop the pending-plan span and this reddens.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: {
        instances: [],
        tabs: [],
        paused_until: null,
        resume_pending: true,
        pending_plan: {
          since: 1,
          plan: { relocations: 12, phase_b_completions: 3, closures: 40, deferred: { x: 2 } },
        },
      },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const plan = wrapper.find('[data-role="pending-plan"]');
    expect(plan.exists()).toBe(true);
    expect(plan.text()).toContain("переселений 12");
    expect(plan.text()).toContain("закрытий 40");
    expect(plan.text()).toContain("отложено 2");
  });
});

// --- §9: the promised "merge windows now" button ------------------------------
describe("merge windows now (§9)", () => {
  it("renders a merge button per instance and shows the {merged} answer", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [
            { id: "prox", title: "Prox", connected: true, snapshot_at: 1_000_000, last_seen_at: 1_000_000 },
          ],
          tabs: [],
          quick_links: [],
          server_now: 1_000_000,
        },
      },
      merge: { status: 200, body: { merged: 4 } },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const btn = wrapper.find('[data-role="merge-windows"]');
    expect(btn.exists()).toBe(true);
    await btn.trigger("click");
    await flushPromises();

    expect(wrapper.find('[data-role="merge-result"]').text()).toContain("4");
  });
});

// --- §7: a 423 is the emergency stop working, with the human's way past it -----
describe("pause gate override (§7)", () => {
  it("names the pause and offers the force button after a 423", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [
            { id: "other", title: "Other", connected: true, snapshot_at: 1_000_000, last_seen_at: 1_000_000 },
          ],
          tabs: [{ instance_id: "other", tab_id: 7, url: "https://f/x", title: "FX" }],
          quick_links: [],
          server_now: 1_000_000,
        },
      },
      focus: (opts, n) => {
        bodies.push(JSON.parse(opts.body));
        return n === 1
          ? { status: 423, body: { error: "paused", until: 1_003_600_000 } }
          : { status: 200, body: { ok: true } };
      },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    await wrapper.find('[data-role="foreign-group"] .sp-item').trigger("click");
    await flushPromises();

    const block = wrapper.find('[data-role="pause-block"]');
    expect(block.exists()).toBe(true);
    expect(block.text()).toContain("на паузе");
    // The generic "переключитесь вручную" must NOT be what the human is told here.
    expect(wrapper.find(".sp-fallback").text()).not.toContain("вручную");

    await wrapper.find('[data-role="pause-force"]').trigger("click");
    await flushPromises();
    expect(bodies[1].force).toBe(true);
    expect(wrapper.find('[data-role="pause-block"]').exists()).toBe(false);
  });
});
