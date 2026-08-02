import { describe, it, expect, vi } from "vitest";
import {
  httpBaseFromServiceUrl,
  buildRule,
  requestPreview,
  saveRule,
  init,
} from "../pages/popup.js";

const CONFIG = {
  instanceId: "prox",
  title: "Prox",
  serviceUrl: "wss://host.example/",
  token: "the-token",
};

// --- pure helpers -----------------------------------------------------------
describe("httpBaseFromServiceUrl", () => {
  it("maps the wss socket URL to the https API base and trims slashes", () => {
    expect(httpBaseFromServiceUrl("wss://host.example/")).toBe("https://host.example");
    expect(httpBaseFromServiceUrl("ws://host.example")).toBe("http://host.example");
    expect(httpBaseFromServiceUrl("https://host.example")).toBe("https://host.example");
  });
});

describe("buildRule", () => {
  it("builds a host pattern (NOT a full URL) and an <origin>/* label", () => {
    const rule = buildRule("https://borneo.lc/a/b?q=1#f", "prox");
    // The stored pattern is the HOST — a full URL would be rejected 422 by §8.
    expect(rule.pattern).toBe("borneo.lc");
    expect(rule.instance_id).toBe("prox");
    expect(rule.label).toBe("https://borneo.lc/*");
  });

  it("keeps an explicit non-default port in the pattern", () => {
    expect(buildRule("https://grafana.lc:3000/d", "prox").pattern).toBe("grafana.lc:3000");
  });

  it("rejects a non-http(s) tab", () => {
    expect(() => buildRule("chrome://settings", "prox")).toThrow();
  });
});

// --- network helpers use the server matcher (§8) ----------------------------
describe("requestPreview / saveRule", () => {
  it("previews with a Bearer token and the host pattern body", async () => {
    const fetchFn = vi.fn(async () => ({ json: async () => ({ relocations: 2, closures: 0 }) }));
    const rule = { pattern: "borneo.lc", instance_id: "prox" };
    const payload = await requestPreview(fetchFn, "https://host.example", "tok", rule);
    expect(payload).toEqual({ relocations: 2, closures: 0 });
    const [url, opts] = fetchFn.mock.calls[0];
    expect(url).toBe("https://host.example/api/rules/preview");
    expect(opts.method).toBe("POST");
    expect(opts.headers.Authorization).toBe("Bearer tok");
    expect(JSON.parse(opts.body)).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
  });

  it("saves with confirm_impact:true (the user has seen the preview)", async () => {
    const fetchFn = vi.fn(async () => ({ status: 201, json: async () => ({ ok: true, id: 7 }) }));
    const rule = { pattern: "borneo.lc", instance_id: "prox" };
    const res = await saveRule(fetchFn, "https://host.example", "tok", rule);
    expect(res.status).toBe(201);
    const [url, opts] = fetchFn.mock.calls[0];
    expect(url).toBe("https://host.example/api/rules");
    expect(JSON.parse(opts.body)).toEqual({
      pattern: "borneo.lc",
      instance_id: "prox",
      confirm_impact: true,
    });
  });
});

// --- full popup flow: build -> preview -> save ------------------------------
function fakeEl() {
  return {
    textContent: "",
    disabled: false,
    _click: null,
    addEventListener(type, fn) {
      if (type === "click") this._click = fn;
    },
  };
}

function fakeDoc() {
  const els = {};
  for (const id of ["origin", "pattern", "target", "impact", "save", "status"]) {
    els[id] = fakeEl();
  }
  return { els, getElementById: (id) => els[id] };
}

function routedFetch(preview) {
  const calls = [];
  const fetchFn = vi.fn(async (url, opts) => {
    calls.push({ url, opts });
    if (url.endsWith("instance.json")) return { json: async () => ({ ...CONFIG }) };
    if (url.endsWith("/api/rules/preview")) return { json: async () => preview };
    if (url.endsWith("/api/rules")) return { status: 201, json: async () => ({ ok: true, id: 9 }) };
    throw new Error("unexpected fetch " + url);
  });
  return { fetchFn, calls };
}

describe("init: build -> preview -> save", () => {
  it("builds the <origin>/* rule for THIS instance, previews it, and saves on click", async () => {
    const doc = fakeDoc();
    const chromeApi = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p },
      tabs: { query: async () => [{ id: 1, url: "https://borneo.lc/dashboard" }] },
    };
    const preview = {
      relocations: 3,
      closures: 1,
      instances: [
        { id: "prox", counted: true },
        { id: "old", counted: false }, // freshness surfaced to the human
      ],
    };
    const { fetchFn, calls } = routedFetch(preview);

    await init(doc, chromeApi, fetchFn);

    // Built the rule for the current instance and rendered it.
    expect(doc.els.origin.textContent).toBe("https://borneo.lc/*");
    expect(doc.els.pattern.textContent).toBe("borneo.lc");
    expect(doc.els.target.textContent).toBe("prox");
    // Previewed via the server matcher; impact + "not counted" surfaced.
    const previewCall = calls.find((c) => c.url.endsWith("/api/rules/preview"));
    expect(JSON.parse(previewCall.opts.body)).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
    expect(doc.els.impact.textContent).toContain("relocate 3");
    expect(doc.els.impact.textContent).toContain("close 1");
    expect(doc.els.impact.textContent).toContain("old"); // not-counted instance named

    // Save is enabled; clicking it POSTs the rule with confirm_impact.
    expect(doc.els.save.disabled).toBe(false);
    await doc.els.save._click();
    const saveCall = calls.find((c) => c.url.endsWith("/api/rules") && c.opts.method === "POST");
    expect(JSON.parse(saveCall.opts.body)).toEqual({
      pattern: "borneo.lc",
      instance_id: "prox",
      confirm_impact: true,
    });
    expect(doc.els.status.textContent).toBe("Rule saved.");
  });

  it("disables save when the tab cannot become a rule (non-http)", async () => {
    const doc = fakeDoc();
    const chromeApi = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p },
      tabs: { query: async () => [{ id: 1, url: "chrome://settings" }] },
    };
    const { fetchFn } = routedFetch({ relocations: 0, closures: 0 });
    await init(doc, chromeApi, fetchFn);
    expect(doc.els.save.disabled).toBe(true);
    expect(doc.els.status.textContent).toContain("Cannot build a rule");
  });
});
