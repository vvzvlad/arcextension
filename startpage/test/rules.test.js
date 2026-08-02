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
});
