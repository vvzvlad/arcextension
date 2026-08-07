import { describe, it, expect } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

import App from "../src/App.vue";
import { formatDateTime } from "../src/lib/status.js";
import { makeChrome, makeClipboard, makeFetch } from "./mocks.js";

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
    // The stop ROW renders offline-first (§7): running → "active" + a Стоп button.
    expect(wrapper.find('[data-role="pause-row"]').exists()).toBe(true);
    expect(wrapper.find('[data-role="pause-start"]').exists()).toBe(true);
    expect(wrapper.find('[data-role="pause-start"]').text()).toBe("Стоп");
    // Offline indicator is shown.
    expect(wrapper.find(".sp-header").text()).toContain("офлайн");
  });

  it("renders the bookmarks and history columns from the LOCAL chrome APIs", async () => {
    // Both are local sources like chrome.tabs.query: no network is involved, so they
    // must be on screen on the very first paint (§10) — this test has no /api/state.
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 1, url: "https://own/a", title: "Own A" }],
      messages: { get_identity: { instanceId: "me" } },
      bookmarks: [
        {
          id: "1",
          title: "Панель закладок",
          children: [
            {
              id: "11",
              title: "Команда Obsidian выпустила веб-клипер / Habr",
              url: "https://habr.com/ru/news/1/",
            },
          ],
        },
      ],
      history: [
        { url: "https://example.com/x", title: "Пример страницы", lastVisitTime: Date.now() - 60_000 },
      ],
    });
    const { fetchFn } = makeFetch({ state: undefined }); // offline

    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => Date.now() } },
    });
    await flushPromises();

    const bookmarks = wrapper.find('[data-role="bookmarks"]');
    expect(bookmarks.exists()).toBe(true);
    expect(bookmarks.text()).toContain("Панель закладок"); // the folder is the section
    // …and the row carries the CLEANED title (the site tail is cut, the rest is kept).
    expect(bookmarks.text()).toContain("Команда Obsidian выпустила веб-клипер");
    expect(bookmarks.text()).not.toContain("/ Habr");

    const history = wrapper.find('[data-role="history"]');
    expect(history.exists()).toBe(true);
    expect(history.text()).toContain("Пример страницы");
    expect(history.text()).toContain("Сегодня"); // grouped by local day
  });

  it("puts the FULL url on every row as a tooltip", async () => {
    // The visible label is cleaned AND truncated by text-overflow, so two rows can look
    // identical. The native tooltip is the only place the whole address survives.
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 1, url: "https://own.example/very/long/path?q=1", title: "Own A" }],
      messages: { get_identity: { instanceId: "me" } },
      bookmarks: [{ id: "1", title: "Панель", children: [{ id: "11", title: "H", url: "https://habr.com/x" }] }],
      history: [{ url: "https://example.com/x", title: "Пример", lastVisitTime: Date.now() - 60_000 }],
    });
    const { fetchFn } = makeFetch({ state: undefined });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => Date.now() } },
    });
    await flushPromises();

    expect(wrapper.find('[data-role="own-tabs"] .sp-item').attributes("title")).toBe(
      "https://own.example/very/long/path?q=1",
    );
    expect(
      wrapper.find('[data-role="bookmarks"] .sp-item-title').attributes("title"),
    ).toBe("https://habr.com/x");
    expect(wrapper.find('[data-role="history"] .sp-item-title').attributes("title")).toBe(
      "https://example.com/x",
    );
  });

  it("gives each host its own swatch colour regardless of letter order", async () => {
    // The hash used to reduce to sum(charCodes) % 10, so every anagram of a host landed
    // on the same colour. Any permutation must be able to differ.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({ state: undefined });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const { swatchColor } = wrapper.vm;
    // Eight ANAGRAMS of one host. The old hash mapped every one of them to the SAME
    // colour (a `% 100000` before a `% 10` reduced it to a digit sum), so this set
    // produced exactly one colour; a hash that uses the letter ORDER spreads them.
    const anagrams = [
      "habr.com",
      "crab.hom",
      "brah.com",
      "bahr.com",
      "arbh.com",
      "hbar.com",
      "rabh.com",
      "bhar.com",
    ];
    expect(new Set(anagrams.map(swatchColor)).size).toBeGreaterThan(2);
    // …and it is still deterministic: the same host always gets the same colour.
    expect(swatchColor("habr.com")).toBe(swatchColor("habr.com"));
    // A host-less row still gets its neutral swatch rather than an undefined colour.
    expect(swatchColor("")).toBe(swatchColor(null));
  });

  it("parses NO urls while re-rendering — host/title/swatch are computed once per row", async () => {
    // The page re-renders every second (store.tick() moves the clock the pause countdown
    // and the instance states read), so anything the TEMPLATE computes per row is paid
    // for every second, forever, on an idle newtab. The template used to call
    // `swatchColor(hostOf(url))`, `cleanTitle(title, url)` (which parses again inside)
    // and `hostOf(url)` on each row: three `new URL` per row, measured at 1276 parses
    // per tick on a 452-row profile. Put any of them back in the template and this
    // reddens.
    const env = makeChrome({
      tabs: [
        { id: 1, windowId: 1, url: "https://a.example/1", title: "A1" },
        { id: 2, windowId: 2, url: "https://b.example/2", title: "B2" },
      ],
      messages: { get_identity: { instanceId: "me" } },
      bookmarks: [{ id: "1", title: "Панель", children: [{ id: "11", title: "H", url: "https://habr.com/x" }] }],
      history: [{ url: "https://example.com/x", title: "Пример", lastVisitTime: Date.now() - 60_000 }],
    });
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [
            { id: "other", connected: true, snapshot_at: 1_000_000, last_seen_at: 1_000_000 },
          ],
          tabs: [{ instance_id: "other", tab_id: 7, url: "https://f.example/x", title: "FX" }],
          quick_links: [{ id: 1, url: "https://q.example/", title: "Q", position: 0 }],
          server_now: 1_000_000,
        },
      },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();
    // Every column has rows, so a vacuous "nothing rendered" pass is impossible.
    expect(wrapper.findAll(".sp-item").length).toBeGreaterThan(4);

    const RealURL = globalThis.URL;
    let parses = 0;
    class CountingURL extends RealURL {
      constructor(...args) {
        parses += 1;
        super(...args);
      }
    }
    globalThis.URL = CountingURL;
    try {
      wrapper.vm.store.tick();
      wrapper.vm.$forceUpdate(); // the whole render function runs again
      await flushPromises();
    } finally {
      globalThis.URL = RealURL;
    }
    expect(parses).toBe(0);

    wrapper.unmount();
  });

  it("groups own tabs into WINDOW sections with a host summary", async () => {
    const env = makeChrome({
      tabs: [
        { id: 1, windowId: 1, url: "https://avito.ru/a", title: "A" },
        { id: 2, windowId: 1, url: "https://avito.ru/b", title: "B" },
        { id: 3, windowId: 1, url: "https://other.com/c", title: "C" },
        { id: 4, windowId: 2, url: "https://habr.com/d", title: "D" },
      ],
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({ state: undefined });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const own = wrapper.find('[data-role="own-tabs"]');
    expect(own.findAll(".sp-sec")).toHaveLength(2); // two windows, two sections
    expect(own.text()).toContain("avito.ru — 2 из 3");
    expect(own.text()).toContain("Окно 2");
  });

  it("stays NON-EMPTY when chrome.bookmarks / chrome.history are missing", async () => {
    // An older Chrome, or a manifest with the two permissions removed. The optional
    // columns render empty; nothing else on the page may be affected, and nothing
    // may throw out of init() (offline-first, §10).
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 1, url: "https://own/a", title: "Own A" }],
      messages: { get_identity: { instanceId: "me" } },
      withoutOptionalApis: true,
    });
    expect(env.chrome.bookmarks).toBeUndefined();
    expect(env.chrome.history).toBeUndefined();
    const { fetchFn } = makeFetch({ state: undefined });

    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    expect(wrapper.html().trim().length).toBeGreaterThan(0);
    expect(wrapper.find('[data-role="own-tabs"]').text()).toContain("Own A");
    expect(wrapper.find('[data-role="quick-links"]').exists()).toBe(true);
    expect(wrapper.find('[data-role="bookmarks"]').exists()).toBe(true);
    expect(wrapper.find('[data-role="history"]').text()).toContain("История пуста");
    expect(wrapper.find('[data-role="status-bar"]').exists()).toBe(true);
  });

  it("keeps the window labels put while the search box filters the list", async () => {
    // The header numbers the WINDOW, not its position in the filtered view: typing in
    // the search box used to renumber the sections under the human's eyes.
    const env = makeChrome({
      tabs: [
        { id: 1, windowId: 5, url: "https://a.com/1", title: "A1" },
        { id: 2, windowId: 6, url: "https://b.com/1", title: "B1" },
        { id: 3, windowId: 7, url: "https://habr.com/1", title: "Хабр" },
      ],
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({ state: undefined });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const own = () => wrapper.find('[data-role="own-tabs"]');
    expect(own().findAll(".sp-sec")).toHaveLength(3);
    expect(own().text()).toContain("Окно 3");
    // The first window is NOT relabelled "Этот браузер" — the card already says so.
    expect(own().text()).toContain("Окно 1");

    await wrapper.find("input.sp-search").setValue("habr");
    await flushPromises();

    const sections = own().findAll(".sp-sec");
    expect(sections).toHaveLength(1);
    expect(sections[0].text()).toContain("Окно 3"); // still the THIRD window
    expect(sections[0].text()).not.toContain("Окно 1");
  });

  async function statusRow(state) {
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 1, url: "https://own/a", title: "Own A" }],
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({ state: { instances: [], tabs: [], ...state } });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();
    return wrapper;
  }

  it("shows the STOPPED row with the since-stamp and a Старт button (§7)", async () => {
    // stopped_at != null = the automation is stopped INDEFINITELY: no deadline, no
    // countdown — a "since" stamp and the one button that brings it back.
    const wrapper = await statusRow({ stopped_at: 999_000, server_now: 1_000_000 });

    const rowText = wrapper.find('[data-role="pause-row"]').text();
    expect(rowText).toContain("Остановлено");
    expect(rowText).not.toContain("Автоматика активна");
    // Date AND time: an indefinite stop can span days, so a bare time-of-day would
    // read as "today" however old the stop is.
    expect(wrapper.find('[data-role="pause-since"]').text()).toMatch(
      /с \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}/,
    );
    const btn = wrapper.find('[data-role="pause-resume"]');
    expect(btn.exists()).toBe(true);
    expect(btn.text()).toBe("Старт");
  });

  it("«Старт» is disabled while the resume DELETE is in flight (double-click guard, §7)", async () => {
    // The DELETE spans the whole confirming pass; a second click meanwhile would
    // arrive after the stop cleared server-side and confirm a plan the human never
    // saw. So the button must carry `disabled` for the full in-flight window.
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
    });
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    const { fetchFn, counts } = makeFetch({
      state: { instances: [], tabs: [], stopped_at: 999_000 },
      pauseDelete: async () => {
        await gate; // park the DELETE in flight
        return { status: 200, body: { resumed: true } };
      },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const btn = wrapper.find('[data-role="pause-resume"]');
    expect(btn.attributes("disabled")).toBeUndefined();
    await btn.trigger("click");
    expect(counts.pauseDelete).toBe(1);
    expect(btn.attributes("disabled")).toBeDefined(); // in flight → disabled
    await btn.trigger("click"); // a second click must not send a second DELETE
    expect(counts.pauseDelete).toBe(1);

    release();
    await flushPromises();
  });

  it("stopped + armed latch renders the deferred plan counts (§7)", async () => {
    // «Старт» resumes through the NORMAL gate and does NOT confirm the latched plan,
    // so the plan must be VISIBLE while stopped — the informed confirm is the NEXT
    // click, and a human cannot be informed by a hidden plan.
    const wrapper = await statusRow({
      stopped_at: 999_000,
      server_now: 1_000_000,
      resume_pending: true,
      pending_plan: {
        since: 1,
        plan: { relocations: 12, closures: 40, deferred: { x: 2 }, total: 52, threshold: 20 },
      },
    });

    const row = wrapper.find('[data-role="pause-row"]');
    expect(row.text()).toContain("Остановлено");
    const plan = row.find('[data-role="pending-plan"]');
    expect(plan.exists()).toBe(true);
    expect(plan.text()).toContain("Переселений 12");
    expect(plan.text()).toContain("закрытий 40");
    expect(plan.text()).toContain("отложено 2");
    // One short sub, not a wall of text: what the counts are waiting for.
    expect(plan.text()).toContain("ждёт подтверждения после старта");
    // The button stays «Старт» — «Выполнить» appears only once the stop is lifted.
    expect(row.find('[data-role="pause-resume"]').text()).toBe("Старт");
    expect(row.find('[data-role="pause-confirm"]').exists()).toBe(false);
  });

  it("shows the over-threshold LATCH with the plan counts and a Выполнить button (§7)", async () => {
    // resume_pending has exactly ONE meaning now: the plan exceeded
    // MAX_ACTIONS_PER_PASS and waits for one confirming click. The human confirms
    // SEEING the counts — a bare boolean asks for a blind click on the largest salvo
    // the system ever fires. Drop the latch branch and this reddens.
    const wrapper = await statusRow({
      resume_pending: true,
      pending_plan: {
        since: 1,
        plan: {
          relocations: 12,
          phase_b_completions: 3,
          closures: 40,
          deferred: { x: 2 },
          total: 52,
          threshold: 20,
        },
      },
    });

    expect(wrapper.find('[data-role="pause-pending"]').exists()).toBe(true);
    const rowText = wrapper.find('[data-role="pause-row"]').text();
    expect(rowText).toContain("Ждёт подтверждения");
    expect(rowText).toContain("план 52 действий при пороге 20");
    expect(rowText).not.toContain("Автоматика активна");
    // The counts line is SHORT — counts only, no explain wall.
    const plan = wrapper.find('[data-role="pending-plan"]');
    expect(plan.exists()).toBe(true);
    expect(plan.text()).toContain("Переселений 12");
    expect(plan.text()).toContain("закрытий 40");
    expect(plan.text()).toContain("отложено 2");
    const btn = wrapper.find('[data-role="pause-confirm"]');
    expect(btn.exists()).toBe(true);
    expect(btn.text()).toBe("Выполнить");
  });

  it("a STOP wins over the latch, and running shows the Стоп button (§7)", async () => {
    // Stopped + latched at once renders as stopped: the stop is the stronger fact —
    // nothing runs at all, so "ждёт подтверждения" would understate it.
    const both = await statusRow({ stopped_at: 999_000, resume_pending: true });
    expect(both.find('[data-role="pause-row"]').text()).toContain("Остановлено");
    expect(both.find('[data-role="pause-confirm"]').exists()).toBe(false);

    const running = await statusRow({ stopped_at: null });
    expect(running.find('[data-role="pause-row"]').text()).toContain("Автоматика активна");
    expect(running.find('[data-role="pause-start"]').text()).toBe("Стоп");
  });
});

// --- the stopped row's date+time stamp (§7) -----------------------------------
describe("formatDateTime (status.js)", () => {
  it("renders date AND time, zero-padded, deterministically", () => {
    // Built from LOCAL date components so the expectation is timezone-independent.
    const ts = new Date(2026, 0, 5, 9, 3).getTime();
    expect(formatDateTime(ts)).toBe("05.01.2026 09:03");
  });

  it("answers — for null/invalid input, like formatTime", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime(NaN)).toBe("—");
  });
});

// --- the favourites column edits the BROWSER's bookmarks ----------------------
// Every one of these edits is irreversible from this page's side: chrome.bookmarks has
// no trash, and the store's rollback only fires when chrome REFUSES the write.
describe("bookmark editing (§10 favourites column)", () => {
  const TREE = [
    {
      id: "1",
      title: "Панель закладок",
      children: [
        { id: "11", title: "Хабр / Habr", url: "https://habr.com/" },
        { id: "12", title: "MDN", url: "https://developer.mozilla.org/" },
      ],
    },
  ];

  function mountWithBookmarks(extra = {}) {
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      bookmarks: structuredClone(TREE),
      ...extra,
    });
    const { fetchFn } = makeFetch({ state: undefined });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
      attachTo: document.body,
    });
    return { env, wrapper };
  }

  // The armed row IGNORES a confirming click that lands within ~350 ms of the arming
  // one: arming is synchronous here, so a plain double-click would otherwise arm and
  // fire inside one gesture. The guard is real elapsed time (a human's reaction, not a
  // service timestamp), so these tests wait it out for real.
  const pastDeadTime = () => new Promise((r) => setTimeout(r, 400));

  it("needs TWO clicks to delete a bookmark, and says so between them", async () => {
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    const removeBtn = () => wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0];
    const row = () => wrapper.findAll('[data-role="bookmarks"] .sp-item')[0];
    expect(removeBtn().text()).toBe("×");
    expect(row().classes()).not.toContain("is-armed");

    // First click ARMS the row — nothing is deleted yet.
    await removeBtn().trigger("click");
    await flushPromises();
    expect(env.calls.bookmarkRemove).toHaveLength(0);
    expect(removeBtn().text()).toContain("подтвердить");
    expect(removeBtn().classes()).toContain("is-confirm");
    // …and the row says so OUTSIDE hover: .sp-item-actions is `opacity: 0` until the row
    // is hovered, so without this class the only "the next click deletes" signal on the
    // page disappears the moment the mouse leaves the row.
    expect(row().classes()).toContain("is-armed");

    // The second click on the SAME row confirms.
    await pastDeadTime();
    await removeBtn().trigger("click");
    await flushPromises();
    expect(env.calls.bookmarkRemove).toEqual(["11"]);
    expect(wrapper.find('[data-role="bookmarks"]').text()).not.toContain("Хабр");
  });

  it("a DOUBLE-CLICK on × does not delete — it only arms, and stays armed", async () => {
    // Two clicks milliseconds apart are ONE gesture, not two decisions. Arming is
    // synchronous (unlike the rules list, where the first click waits on a server 409),
    // so without a dead time a double-click removes the bookmark from the BROWSER with
    // no frame anyone could have seen — and chrome.bookmarks has no undo.
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    const removeBtn = () => wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0];
    await removeBtn().trigger("click");
    await removeBtn().trigger("click"); // the second half of the double-click
    await flushPromises();

    expect(env.calls.bookmarkRemove).toHaveLength(0);
    // Still armed, so a DELIBERATE second click finishes the job.
    expect(removeBtn().text()).toContain("подтвердить");
    await pastDeadTime();
    await removeBtn().trigger("click");
    await flushPromises();
    expect(env.calls.bookmarkRemove).toEqual(["11"]);
  });

  it("typing in the search box disarms an armed delete", async () => {
    // The armed row can be filtered out from under the human and come back armed, so
    // the next single click on it would delete. The rules editor disarms on any draft
    // edit for the same reason.
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    await wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0].trigger("click");
    expect(wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0].text()).toContain(
      "подтвердить",
    );

    await wrapper.find("input.sp-search").setValue("хабр");
    await flushPromises();
    await pastDeadTime();

    const btn = wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0];
    expect(btn.text()).toBe("×"); // disarmed
    await btn.trigger("click"); // …so this click only re-arms
    await flushPromises();
    expect(env.calls.bookmarkRemove).toHaveLength(0);
  });

  it("opening the add-a-bookmark form disarms an armed delete", async () => {
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    await wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0].trigger("click");
    await wrapper.find('[data-role="bookmark-add-toggle"]').trigger("click");
    await flushPromises();
    await pastDeadTime();

    const btn = wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0];
    expect(btn.text()).toBe("×");
    await btn.trigger("click");
    await flushPromises();
    expect(env.calls.bookmarkRemove).toHaveLength(0);
  });

  it("arming one row and clicking another only re-arms — it never deletes both", async () => {
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    const buttons = () => wrapper.findAll('[data-role="bookmarks"] .sp-remove');
    await buttons()[0].trigger("click"); // arm "Хабр"
    await buttons()[1].trigger("click"); // …then click MDN's ×
    await flushPromises();

    expect(env.calls.bookmarkRemove).toHaveLength(0);
    expect(buttons()[0].text()).toBe("×"); // the first row disarmed
    expect(buttons()[1].text()).toContain("подтвердить"); // the second is armed now
  });

  it("starting a rename disarms a delete armed on another row", async () => {
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    await wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0].trigger("click"); // arm
    await wrapper.findAll('[data-role="bookmarks"] .sp-icon-btn')[1].trigger("click"); // ✎ on MDN
    await flushPromises();

    // Back to a plain "×": the armed delete did not survive the human moving on.
    expect(wrapper.findAll('[data-role="bookmarks"] .sp-remove')[0].text()).toBe("×");
    expect(env.calls.bookmarkRemove).toHaveLength(0);
  });

  it("FOCUSES the rename field, saves on Enter and cancels on Esc", async () => {
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    // ✎ on the first bookmark (the pencil is the first .sp-icon-btn inside the row;
    // index 0 is the section's own "+" button).
    const pencils = () => wrapper.findAll('[data-role="bookmarks"] .sp-item .sp-icon-btn');
    await pencils()[0].trigger("click");
    await flushPromises();

    const input = wrapper.find('[data-role="bookmark-rename"]');
    expect(input.exists()).toBe(true);
    // The ✎ button is gone from the DOM, so without an explicit focus() nothing holds
    // the focus and @blur could never fire.
    expect(document.activeElement).toBe(input.element);

    await input.setValue("Хабр");
    await input.trigger("keyup.enter");
    await flushPromises();
    expect(env.calls.bookmarkUpdate).toEqual([["11", { title: "Хабр" }]]);
    // Enter also closes the editor — and the blur that follows must NOT write again.
    expect(wrapper.find('[data-role="bookmark-rename"]').exists()).toBe(false);

    // Esc leaves the title alone.
    await pencils()[1].trigger("click");
    await flushPromises();
    const second = wrapper.find('[data-role="bookmark-rename"]');
    await second.setValue("Что-то другое");
    await second.trigger("keyup.esc");
    await flushPromises();
    expect(env.calls.bookmarkUpdate).toHaveLength(1); // still just the Enter write
    expect(wrapper.find('[data-role="bookmarks"]').text()).toContain("MDN");

    wrapper.unmount();
  });

  it("SAVES on blur — leaving the field with the mouse commits, exactly once", async () => {
    // Blur is the only remaining way a mouse user commits an edit: `@change` was dropped
    // (it fires for the same gestures Enter/blur already handle and doubled every
    // rename into two chrome.bookmarks writes). Nothing else covers this path, so it
    // rested on reasoning alone.
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    await wrapper.findAll('[data-role="bookmarks"] .sp-item .sp-icon-btn')[0].trigger("click");
    await flushPromises();

    const input = wrapper.find('[data-role="bookmark-rename"]');
    await input.setValue("Хабр");
    await input.trigger("blur");
    await flushPromises();

    expect(env.calls.bookmarkUpdate).toEqual([["11", { title: "Хабр" }]]);
    expect(wrapper.find('[data-role="bookmark-rename"]').exists()).toBe(false);
    expect(wrapper.find('[data-role="bookmarks"]').text()).toContain("Хабр");

    wrapper.unmount();
  });

  it("Esc THEN blur writes nothing — the cancel is not overruled by the blur it causes", async () => {
    // Esc closes the editor, which unmounts the input, which blurs it: the blur handler
    // arrives with the edited value AFTER the cancel. The editingBookmarkId latch is
    // what makes that second arrival a no-op — drop it and Esc silently saves.
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    await wrapper.findAll('[data-role="bookmarks"] .sp-item .sp-icon-btn')[0].trigger("click");
    await flushPromises();

    const input = wrapper.find('[data-role="bookmark-rename"]');
    await input.setValue("Что-то другое");
    await input.trigger("keyup.esc");
    await input.trigger("blur"); // the blur that follows the cancel
    await flushPromises();

    expect(env.calls.bookmarkUpdate).toHaveLength(0);
    expect(wrapper.find('[data-role="bookmarks"]').text()).toContain("Хабр");

    wrapper.unmount();
  });

  it("an EMPTY rename is a rename to the default name, not a silent no-op", async () => {
    // Clearing the field and pressing Enter used to close the editor and change nothing —
    // indistinguishable from Esc, with no way to tell a refusal from a lost keystroke.
    // An empty title now commits the name the row would show anyway: the host.
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    await wrapper.findAll('[data-role="bookmarks"] .sp-item .sp-icon-btn')[0].trigger("click");
    await flushPromises();

    const input = wrapper.find('[data-role="bookmark-rename"]');
    await input.setValue("   ");
    await input.trigger("keyup.enter");
    await flushPromises();

    expect(env.calls.bookmarkUpdate).toEqual([["11", { title: "habr.com" }]]);
    expect(wrapper.find('[data-role="bookmarks"]').text()).toContain("habr.com");

    wrapper.unmount();
  });

  it("says «Нет закладок» instead of showing a section that just stops", async () => {
    // The other three lists all have an empty state; without one here a fresh profile —
    // or a search that filtered everything out — looks like a column that failed to load.
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
      bookmarks: [],
    });
    const { fetchFn } = makeFetch({ state: undefined });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();
    expect(wrapper.find('[data-role="bookmarks"]').text()).toContain("Нет закладок");
    wrapper.unmount();

    // …and the same when a search empties the list rather than the profile.
    const { wrapper: full } = mountWithBookmarks();
    await flushPromises();
    expect(full.find('[data-role="bookmarks"]').text()).not.toContain("Нет закладок");
    await full.find("input.sp-search").setValue("ничего-такого-нет");
    await flushPromises();
    expect(full.find('[data-role="bookmarks"]').text()).toContain("Нет закладок");
    full.unmount();
  });

  it("attaches NO bookmark listeners when the page is closed before init() lands", async () => {
    // onMounted is async: a newtab closed (or navigated away from) while chrome is still
    // answering runs onUnmounted FIRST, and the subscription line then executes into a
    // dead component — listeners nothing will ever detach, one more set per aborted mount.
    const { env, wrapper } = mountWithBookmarks();
    wrapper.unmount(); // …before flushPromises: init() is still in flight
    await flushPromises();

    expect(env.bookmarkListenerCount()).toBe(0);
  });

  it("adds a bookmark from the column's own form (the write permission is reachable)", async () => {
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();

    // The form is COLLAPSED, not absent — the same CSS-only contract the ql-add form has.
    const form = wrapper.find('[data-role="bookmark-add"]');
    expect(form.exists()).toBe(true);
    expect(form.classes()).toContain("is-collapsed");

    await wrapper.find('[data-role="bookmark-add-toggle"]').trigger("click");
    expect(wrapper.find('[data-role="bookmark-add"]').classes()).not.toContain("is-collapsed");

    await form.find('input[name="url"]').setValue("https://new.example/");
    await form.find('input[name="title"]').setValue("Новая");
    await form.trigger("submit");
    await flushPromises();

    expect(env.calls.bookmarkCreate).toHaveLength(1);
    expect(env.calls.bookmarkCreate[0]).toMatchObject({
      url: "https://new.example/",
      title: "Новая",
      parentId: "1", // the first real folder — the bookmarks bar
    });
    expect(wrapper.find('[data-role="bookmarks"]').text()).toContain("Новая");

    wrapper.unmount();
  });

  it("detaches its chrome.bookmarks listeners when the page goes away", async () => {
    const { env, wrapper } = mountWithBookmarks();
    await flushPromises();
    expect(env.bookmarkListenerCount()).toBe(4);

    wrapper.unmount();
    expect(env.bookmarkListenerCount()).toBe(0);
  });
});

// --- §62 item 2: the «Слить окна» button is GONE from the startpage -----------
describe("no merge button on the startpage (§62 item 2)", () => {
  it("renders no merge-windows button (the server endpoint stays, the UI does not)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [
            { id: "prox", title: "Prox", connected: true, snapshot_at: 1_000_000, last_seen_at: 1_000_000, focused_window_id: 8 },
          ],
          tabs: [], quick_links: [], server_now: 1_000_000,
        },
      },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();
    expect(wrapper.find('[data-role="merge-windows"]').exists()).toBe(false);
  });
});

// --- §62 item 3: clicking a space raises that instance's browser --------------
describe("click a space to raise its browser (§62 item 3)", () => {
  it("a foreign row POSTs /api/focus with {windowId} and no tabId", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn } = makeFetch({
      state: {
        status: 200,
        body: {
          instances: [
            { id: "prox", title: "Prox", connected: true, snapshot_at: 1_000_000, last_seen_at: 1_000_000, focused_window_id: 8 },
          ],
          tabs: [], quick_links: [], server_now: 1_000_000,
        },
      },
      focus: (opts) => {
        bodies.push(JSON.parse(opts.body));
        return { status: 200, body: { ok: true } };
      },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const row = wrapper.find('[data-role="space-row"][data-instance="prox"]');
    expect(row.exists()).toBe(true);
    await row.trigger("click");
    await flushPromises();

    expect(bodies[0]).toEqual({ instance: "prox", windowId: 8 });
  });
});

// --- §62 item 4: clicking a window-title header raises that own window ---------
describe("click a window header to raise the window (§62 item 4)", () => {
  it("focuses the own window and does NOT activate a tab", async () => {
    const env = makeChrome({
      tabs: [{ id: 1, windowId: 5, url: "https://own/a", title: "A", active: true }],
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: 1_000_000 } },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const head = wrapper.find('[data-role="own-window-head"]');
    expect(head.exists()).toBe(true);
    await head.trigger("click");
    await flushPromises();

    expect(env.calls.winUpdate).toEqual([[5, { focused: true }]]);
    expect(env.calls.tabUpdate).toEqual([]); // active tab unchanged (item 4)
  });
});

// --- §62 item 5: «выполнить все правила сейчас» runs a pass --------------------
describe("run all rules now button (§62 item 5)", () => {
  it("POSTs /api/run_pass {run_all:true} on one click", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const bodies = [];
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: 1_000_000 } },
      runPass: (opts) => {
        bodies.push(JSON.parse(opts.body));
        return { status: 200, body: { status: "ok" } };
      },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const btn = wrapper.find('[data-role="run-rules-now"]');
    expect(btn.exists()).toBe(true);
    await btn.trigger("click");
    await flushPromises();

    expect(bodies[0]).toEqual({ run_all: true });
    expect(wrapper.find('[data-role="run-now-result"]').text()).toContain("ok");
  });

  it("shows a non-contradictory note when the pass did not run (e.g. stopped)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: 1_000_000 } },
      runPass: () => ({ status: 200, body: { status: "stopped" } }),
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    await wrapper.find('[data-role="run-rules-now"]').trigger("click");
    await flushPromises();

    // The old wording said «проход запущен: stopped» — a "started: stopped" contradiction.
    const text = wrapper.find('[data-role="run-now-result"]').text();
    expect(text).toContain("не выполнен");
    expect(text).not.toContain("проход запущен");
  });
});

// --- §13: «открыть регистрацию и скопировать код» + the console link -----------
describe("open enrollment window row (§13)", () => {
  it("one click arms the window, copies the code and prints it in the row", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn, counts } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: 1_000_000 } },
      enrollWindow: { status: 200, body: { code: "K7M2PQ", until: 1_600_000, seconds_remaining: 600 } },
    });
    const { clipboard, writes } = makeClipboard();
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000, clipboard } },
    });
    await flushPromises();

    expect(wrapper.find('[data-role="enroll-window-row"]').exists()).toBe(true);
    const btn = wrapper.find('[data-role="open-enrollment"]');
    expect(btn.exists()).toBe(true);
    await btn.trigger("click");
    await flushPromises();

    expect(counts.enrollWindow).toBe(1);
    expect(writes).toEqual(["K7M2PQ"]);
    // The code is PRINTED, not merely copied: the clipboard is not a place the human can
    // look, and the row is (see the fallback case below).
    const result = wrapper.find('[data-role="enroll-window-result"]');
    expect(result.text()).toContain("K7M2PQ");
    expect(result.text()).toContain("10 мин");
    expect(result.text()).toContain("скопирован");
  });

  it("a refused clipboard write still shows the code, with the manual-copy hint", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: 1_000_000 } },
      enrollWindow: { status: 200, body: { code: "R4T9WX", until: 1_600_000, seconds_remaining: 600 } },
    });
    const { clipboard } = makeClipboard({ fails: true });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000, clipboard } },
    });
    await flushPromises();

    await wrapper.find('[data-role="open-enrollment"]').trigger("click");
    await flushPromises();

    const text = wrapper.find('[data-role="enroll-window-result"]').text();
    expect(text).toContain("R4T9WX");
    expect(text).toContain("вручную");
  });

  it("the code LEAVES the row once the window's deadline passes", async () => {
    // A newtab lives for hours. Without this the row would still read «код K7M2PQ, окно
    // открыто на 10 мин, скопирован» an hour after the window closed — three claims, all
    // dead, about a code nothing accepts anymore. The server core refuses to surface a
    // dead code (src/curator/enroll.py: a closed window reads code=None) and the page
    // must not re-introduce what the core refuses to do. The deadline is compared against
    // the SERVER-adjusted clock (clockTick + serverOffset), which the page already keeps.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    let clock = 1_000_000;
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: 1_000_000 } },
      enrollWindow: { status: 200, body: { code: "K7M2PQ", until: 1_600_000, seconds_remaining: 600 } },
    });
    const { clipboard } = makeClipboard();
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => clock, clipboard } },
    });
    await flushPromises();

    await wrapper.find('[data-role="open-enrollment"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-role="enroll-window-result"]').text()).toContain("K7M2PQ");

    // One second past the deadline. The page's own 1 s tick is what re-reads the clock.
    clock = 1_600_001;
    wrapper.vm.store.tick();
    await flushPromises();

    const text = wrapper.find('[data-role="enroll-window-result"]').text();
    expect(text).not.toContain("K7M2PQ");
    expect(text).not.toContain("скопирован");
    expect(text).toContain("окно закрылось");
  });

  it("the Админка link points at this deployment's console", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [], server_now: 1_000_000 } },
    });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    const link = wrapper.find('[data-role="admin-link"]');
    expect(link.exists()).toBe(true);
    expect(link.attributes("href")).toBe("https://host/admin");
  });

  it("no configured address => no link at all (never a dead one)", async () => {
    const env = makeChrome({ tabs: [], credential: null, messages: {} });
    const { fetchFn } = makeFetch({ state: undefined });
    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => 1_000_000 } },
    });
    await flushPromises();

    expect(wrapper.find('[data-role="enroll-window-row"]').exists()).toBe(true);
    expect(wrapper.find('[data-role="admin-link"]').exists()).toBe(false);
  });
});

// --- §7: a 423 is the emergency stop working, with the human's way past it -----
describe("stop gate override (§7)", () => {
  it("names the stop and offers the force button after a 423", async () => {
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
          ? { status: 423, body: { error: "stopped", since: 999_000 } }
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
    expect(block.text()).toContain("остановлен");
    // The generic "переключитесь вручную" must NOT be what the human is told here.
    expect(wrapper.find(".sp-fallback").text()).not.toContain("вручную");

    await wrapper.find('[data-role="pause-force"]').trigger("click");
    await flushPromises();
    expect(bodies[1].force).toBe(true);
    expect(wrapper.find('[data-role="pause-block"]').exists()).toBe(false);
  });
});
