import { describe, it, expect } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

import App from "../src/App.vue";
import { createStore } from "../src/lib/store.js";
import { makeChrome, makeFetch } from "./mocks.js";

const NOW = 1_000_000;

async function onlineStore(routes, extra = {}) {
  // instance.json is routed by makeFetch => init() sets base/token, so the editor is
  // "online" and can list/preview/save.
  const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
  const { fetchFn, counts } = makeFetch({ state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } }, ...routes });
  const store = createStore({ chromeApi: env.chrome, fetchFn, now: () => NOW, ...extra });
  await store.init(); // sets base/token from instance.json
  return { store, counts, env };
}

// --- rules list + invalid highlight (§8/§10) ---------------------------------
describe("rules editor: list + invalid highlight (§10)", () => {
  it("lists rules from GET /api/rules and highlights invalid=1 rows", async () => {
    const rules = [
      { id: 1, pattern: "a.com", instance_id: "themed", singleton: 0, invalid: 0 },
      { id: 2, pattern: "bad(", instance_id: "gone", singleton: 0, invalid: 1 },
    ];
    const env = makeChrome({
      tabs: [],
      messages: { get_identity: { instanceId: "me" } },
    });
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules } },
    });

    const wrapper = mount(App, {
      props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } },
    });
    await flushPromises(); // init + refresh + loadRules

    const rows = wrapper.findAll('[data-role="rules-list"] .sp-rule');
    expect(rows).toHaveLength(2);
    // The invalid rule row carries the highlight class (drop the :class binding and
    // this reddens) — the invalid=0 row does not.
    const valid = rows.find((r) => r.attributes("data-invalid") === "0");
    const invalid = rows.find((r) => r.attributes("data-invalid") === "1");
    expect(valid.classes()).not.toContain("is-invalid");
    expect(invalid.classes()).toContain("is-invalid");
    expect(invalid.text()).toContain("невалидно");
  });
});

// --- preview BEFORE save (§8) ------------------------------------------------
describe("rules editor: preview before save (§8/§10)", () => {
  it("previewRuleDraft populates the impact from POST /api/rules/preview", async () => {
    const { store, counts } = await onlineStore({
      rulesPreview: { status: 200, body: { relocations: 3, closures: 2, impact: 5, requires_confirm: true } },
    });
    await store.loadRules();

    const preview = await store.previewRuleDraft("create", {
      pattern: "x.com",
      instance_id: "themed",
      singleton: false,
    });

    expect(counts.rulesPreview).toBe(1);
    expect(preview.impact).toBe(5);
    expect(store.rulesPreview.value.relocations).toBe(3);
  });

  it("save is GATED: a 409 returns the preview and does NOT commit until confirmed", async () => {
    // First save (no confirm) => 409 carrying the preview. The second save (with
    // confirmImpact) => 201. Proves the confirm gate is honoured before writing.
    const { store, counts } = await onlineStore({
      rulesSave: (opts) => {
        const body = JSON.parse(opts.body);
        if (!body.confirm_impact) {
          return { status: 409, body: { relocations: 4, closures: 0, impact: 4, requires_confirm: true, error: "confirm_impact required" } };
        }
        return { status: 201, body: { ok: true, id: 9 } };
      },
    });
    await store.loadRules();

    const draft = { pattern: "x.com", instance_id: "themed", singleton: false };
    const first = await store.saveRuleDraft("create", draft);
    expect(first.needsConfirm).toBe(true);
    expect(first.preview.impact).toBe(4);
    expect(store.rulesPreview.value.impact).toBe(4); // impact surfaced to the human
    expect(counts.rulesSave).toBe(1); // only the (rejected) first attempt so far

    const second = await store.saveRuleDraft("create", draft, { confirmImpact: true });
    expect(second.ok).toBe(true);
    expect(counts.rulesSave).toBe(2);
  });
});

// --- offline-graceful (§10) --------------------------------------------------
describe("rules editor: offline-graceful (§10)", () => {
  it("with no base/token the editor degrades to an offline note, no throw", async () => {
    // No instance.json route => loadInstanceConfig fails => base/token stay null.
    const env = makeChrome({ tabs: [], messages: {} });
    const fetchFn = async (url) => {
      if (String(url).includes("instance.json")) throw new Error("no config");
      throw new Error("network down");
    };
    const store = createStore({ chromeApi: env.chrome, fetchFn, now: () => NOW });
    await store.init();

    await store.loadRules();
    expect(store.rulesOffline.value).toBe(true);
    expect(store.rulesLoaded.value).toBe(false);

    // Save/preview are no-ops that flag offline rather than throwing.
    const res = await store.saveRuleDraft("create", { pattern: "x", instance_id: "y" });
    expect(res.offline).toBe(true);
    const prev = await store.previewRuleDraft("create", { pattern: "x", instance_id: "y" });
    expect(prev).toBe(null);
  });
});

// --- delete confirm gate (§8): no one-click auto-confirm ---------------------
describe("rules editor: delete confirm gate (§8)", () => {
  it("first click is a probe that arms; only a second click confirms", async () => {
    const rules = [{ id: 5, pattern: "big.com", instance_id: "themed", singleton: 0, invalid: 0 }];
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    let commits = 0;
    let probes = 0;
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules } },
      rulesSave: (opts) => {
        const method = (opts.method || "GET").toUpperCase();
        const body = opts.body ? JSON.parse(opts.body) : {};
        if (method !== "DELETE") return { status: 200, body: { ok: true } };
        if (!body.confirm_impact) {
          probes += 1;
          return { status: 409, body: { relocations: 100, closures: 0 } };
        }
        commits += 1;
        return { status: 200, body: { ok: true } };
      },
    });
    const wrapper = mount(App, { props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } } });
    await flushPromises();
    const btn = wrapper.find('[data-role="rules-list"] .sp-rule .sp-remove');
    await btn.trigger("click");
    await flushPromises();
    // First click PROBES only (409) and arms — nothing deleted. Drop the gate
    // (auto-confirm in one click) and `commits` becomes 1 here => reddens.
    expect(probes).toBe(1);
    expect(commits).toBe(0);
    expect(btn.text()).toContain("подтвердить");
    await btn.trigger("click");
    await flushPromises();
    expect(commits).toBe(1);
  });

  it("editing the draft DISARMS an armed delete (its impact was for the pre-edit rule)", async () => {
    const rules = [{ id: 5, pattern: "big.com", instance_id: "themed", singleton: 0, invalid: 0 }];
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    let commits = 0;
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules } },
      rulesSave: (opts) => {
        const method = (opts.method || "GET").toUpperCase();
        const body = opts.body ? JSON.parse(opts.body) : {};
        if (method !== "DELETE") return { status: 200, body: { ok: true } };
        if (!body.confirm_impact) return { status: 409, body: { relocations: 100, closures: 0 } };
        commits += 1;
        return { status: 200, body: { ok: true } };
      },
    });
    const wrapper = mount(App, { props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } } });
    await flushPromises();

    const btn = wrapper.find('[data-role="rules-list"] .sp-rule .sp-remove');
    await btn.trigger("click"); // arms the delete
    await flushPromises();
    expect(btn.text()).toContain("подтвердить");

    // The human edits the rule instead of confirming.
    await wrapper.find('[data-role="rule-form"] input[name="pattern"]').setValue("other.com");
    await flushPromises();

    expect(btn.text()).not.toContain("подтвердить"); // disarmed
    await btn.trigger("click"); // this is a fresh PROBE, not a confirmation
    await flushPromises();
    expect(commits).toBe(0);
  });
});

// --- §8: a confirmation belongs to the rule that was PREVIEWED ----------------
describe("rules editor: an edited draft cannot ride an old confirmation (§8)", () => {
  it("editing the pattern after a 409 disarms the confirm and drops the stale impact", async () => {
    // The bypass: preview `borneo.lc` (2/0) => 409 arms the button; widen the pattern to
    // `corp.example` (300/120) and click "Подтвердить и сохранить" => confirm_impact goes
    // out for a rule whose impact the server NEVER showed, with 2/0 still on screen. That
    // is the echo-confirmation §8's gate exists to stop.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const saves = [];
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules: [] } },
      rulesSave: (opts) => {
        const body = JSON.parse(opts.body);
        saves.push(body);
        if (!body.confirm_impact) {
          const wide = body.pattern === "corp.example";
          return {
            status: 409,
            body: wide
              ? { relocations: 300, closures: 120, requires_confirm: true }
              : { relocations: 2, closures: 0, requires_confirm: true },
          };
        }
        return { status: 201, body: { ok: true, id: 1 } };
      },
    });
    const wrapper = mount(App, { props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } } });
    await flushPromises();

    const form = wrapper.find('[data-role="rule-form"]');
    await form.find('input[name="pattern"]').setValue("borneo.lc");
    await form.find('input[name="instance_id"]').setValue("themed");
    await form.trigger("submit");
    await flushPromises();

    // Armed on the NARROW rule, its impact shown.
    expect(wrapper.find('[data-role="rule-save"]').text()).toContain("Подтвердить");
    expect(wrapper.find('[data-role="rule-preview-out"]').text()).toContain("2");

    // The human widens the pattern instead of confirming.
    await form.find('input[name="pattern"]').setValue("corp.example");
    await flushPromises();

    // Disarmed, and the now-meaningless 2/0 impact is gone from the screen.
    expect(wrapper.find('[data-role="rule-save"]').text()).toBe("Сохранить");
    expect(wrapper.find('[data-role="rule-preview-out"]').exists()).toBe(false);

    // The next click is a PROBE for the new rule, not a confirmation of the old one.
    await form.trigger("submit");
    await flushPromises();
    expect(saves[1]).toMatchObject({ pattern: "corp.example" });
    expect(saves[1].confirm_impact).toBeUndefined();
    // ...and the impact now on screen is the NEW rule's.
    expect(wrapper.find('[data-role="rule-preview-out"]').text()).toContain("300");

    // Only now can a confirmation go out, and only for the rule that was shown.
    await form.trigger("submit");
    await flushPromises();
    expect(saves[2]).toMatchObject({ pattern: "corp.example", confirm_impact: true });
  });

  it("an edit made WHILE the save is in flight cannot be re-armed by the arriving 409", async () => {
    // THE critical one. The server's whole-pass preview runs for a second or two and
    // `confirmPending` is set AFTER that await, so an edit inside the request window is
    // disarmed by the watcher and then re-armed by the 409 — and the next click sends
    // confirm_impact:true for a rule the server never previewed, with the OLD impact on
    // screen. Remove the revision snapshot in onSave and this reddens.
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const saves = [];
    let releaseFirst;
    const firstInFlight = new Promise((r) => (releaseFirst = r));
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules: [] } },
      rulesSave: async (opts, n) => {
        const body = JSON.parse(opts.body);
        saves.push(body);
        if (n === 1) await firstInFlight; // the slow whole-pass preview
        if (!body.confirm_impact) {
          return {
            status: 409,
            body:
              body.pattern === "corp.example"
                ? { relocations: 300, closures: 120, requires_confirm: true }
                : { relocations: 2, closures: 0, requires_confirm: true },
          };
        }
        return { status: 201, body: { ok: true, id: 1 } };
      },
    });
    const wrapper = mount(App, { props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } } });
    await flushPromises();

    const form = wrapper.find('[data-role="rule-form"]');
    await form.find('input[name="pattern"]').setValue("borneo.lc");
    await form.find('input[name="instance_id"]').setValue("themed");
    form.trigger("submit"); // NOT awaited: the request is parked below
    await flushPromises();

    // The human widens the pattern WHILE the first save is still in flight.
    await form.find('input[name="pattern"]').setValue("corp.example");
    await flushPromises();

    releaseFirst();
    await flushPromises();

    // The 409 for `borneo.lc` must NOT arm a confirm for `corp.example`.
    expect(wrapper.find('[data-role="rule-save"]').text()).toBe("Сохранить");
    // ...and the stale 2/0 impact must not be on screen next to it.
    expect(wrapper.find('[data-role="rule-preview-out"]').exists()).toBe(false);

    // The next click is a PROBE for the new rule, not a blind confirmation.
    await form.trigger("submit");
    await flushPromises();
    expect(saves[1]).toMatchObject({ pattern: "corp.example" });
    expect(saves[1].confirm_impact).toBeUndefined();
    expect(wrapper.find('[data-role="rule-preview-out"]').text()).toContain("300");
  });

  it("changing the TARGET INSTANCE disarms the gate (it changes where tabs go)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const saves = [];
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules: [] } },
      rulesSave: (opts) => {
        const body = JSON.parse(opts.body);
        saves.push(body);
        if (!body.confirm_impact) return { status: 409, body: { relocations: 5, closures: 0 } };
        return { status: 201, body: { ok: true, id: 1 } };
      },
    });
    const wrapper = mount(App, { props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } } });
    await flushPromises();

    const form = wrapper.find('[data-role="rule-form"]');
    await form.find('input[name="pattern"]').setValue("borneo.lc");
    await form.find('input[name="instance_id"]').setValue("themed");
    await form.trigger("submit");
    await flushPromises();
    expect(wrapper.find('[data-role="rule-save"]').text()).toContain("Подтвердить");

    // Same pattern, DIFFERENT destination — the tabs would go somewhere else entirely.
    await form.find('input[name="instance_id"]').setValue("prox");
    await flushPromises();

    expect(wrapper.find('[data-role="rule-save"]').text()).toBe("Сохранить");
    await form.trigger("submit");
    await flushPromises();
    expect(saves[1]).toMatchObject({ instance_id: "prox" });
    expect(saves[1].confirm_impact).toBeUndefined();
  });

  it("a 409 whose body cannot be read does NOT arm the gate (parity with the popup)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const saves = [];
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules: [] } },
      rulesSave: (opts) => {
        saves.push(JSON.parse(opts.body));
        return { status: 409, body: { error: "confirm_impact required" } }; // no numbers
      },
    });
    const wrapper = mount(App, { props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } } });
    await flushPromises();

    const form = wrapper.find('[data-role="rule-form"]');
    await form.find('input[name="pattern"]').setValue("borneo.lc");
    await form.find('input[name="instance_id"]').setValue("themed");
    await form.trigger("submit");
    await flushPromises();

    // No armed button, no "Переселений: undefined" block, and an explanation instead.
    expect(wrapper.find('[data-role="rule-save"]').text()).toBe("Сохранить");
    expect(wrapper.find('[data-role="rule-preview-out"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("не удалось прочитать");

    // A second click still probes; it never confirms blind.
    await form.trigger("submit");
    await flushPromises();
    expect(saves.every((s) => s.confirm_impact === undefined)).toBe(true);
  });

  it("toggling `singleton` disarms too (it changes what the rule does)", async () => {
    const env = makeChrome({ tabs: [], messages: { get_identity: { instanceId: "me" } } });
    const saves = [];
    const { fetchFn } = makeFetch({
      state: { status: 200, body: { instances: [], tabs: [], quick_links: [] } },
      rules: { status: 200, body: { rules: [] } },
      rulesSave: (opts) => {
        const body = JSON.parse(opts.body);
        saves.push(body);
        if (!body.confirm_impact) return { status: 409, body: { relocations: 9, closures: 1 } };
        return { status: 201, body: { ok: true, id: 1 } };
      },
    });
    const wrapper = mount(App, { props: { deps: { chromeApi: env.chrome, fetchFn, now: () => NOW } } });
    await flushPromises();

    const form = wrapper.find('[data-role="rule-form"]');
    await form.find('input[name="pattern"]').setValue("borneo.lc");
    await form.find('input[name="instance_id"]').setValue("themed");
    await form.trigger("submit");
    await flushPromises();
    expect(wrapper.find('[data-role="rule-save"]').text()).toContain("Подтвердить");

    await form.find('input[type="checkbox"]').setValue(true);
    await flushPromises();

    expect(wrapper.find('[data-role="rule-save"]').text()).toBe("Сохранить");
    await form.trigger("submit");
    await flushPromises();
    expect(saves[1].confirm_impact).toBeUndefined();
    expect(saves[1].singleton).toBe(true);
  });
});
