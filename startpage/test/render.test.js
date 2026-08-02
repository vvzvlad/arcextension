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
});
