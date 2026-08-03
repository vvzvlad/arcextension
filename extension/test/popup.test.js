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

  it("saves WITHOUT confirm_impact by default (the §8 gate is not echo-confirmed)", async () => {
    const fetchFn = vi.fn(async () => ({ status: 201, json: async () => ({ ok: true, id: 7 }) }));
    const rule = { pattern: "borneo.lc", instance_id: "prox" };
    const res = await saveRule(fetchFn, "https://host.example", "tok", rule);
    expect(res.status).toBe(201);
    const [url, opts] = fetchFn.mock.calls[0];
    expect(url).toBe("https://host.example/api/rules");
    // No confirm_impact key at all: the human has confirmed nothing yet.
    expect(JSON.parse(opts.body)).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
  });

  it("sends confirm_impact:true only when explicitly asked", async () => {
    const fetchFn = vi.fn(async () => ({ status: 201, json: async () => ({ ok: true, id: 7 }) }));
    const rule = { pattern: "borneo.lc", instance_id: "prox" };
    await saveRule(fetchFn, "https://host.example", "tok", rule, { confirmImpact: true });
    expect(JSON.parse(fetchFn.mock.calls[0][1].body)).toEqual({
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

// `preview` may be an Error (the preview call fails). `save` is a function of the
// call index so a test can answer 409-then-201, mirroring the §8 confirm gate.
function routedFetch(preview, save) {
  const calls = [];
  let saveCalls = 0;
  const fetchFn = vi.fn(async (url, opts) => {
    calls.push({ url, opts });
    if (url.endsWith("instance.json")) return { json: async () => ({ ...CONFIG }) };
    if (url.endsWith("/api/rules/preview")) {
      if (preview instanceof Error) throw preview;
      return { json: async () => preview };
    }
    if (url.endsWith("/api/rules")) {
      saveCalls += 1;
      if (save) return save(saveCalls);
      return { status: 201, json: async () => ({ ok: true, id: 9 }) };
    }
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

    // Save is enabled; the FIRST click posts WITHOUT confirm_impact (§8: the gate is
    // the server's question, and one click must not answer it blind).
    expect(doc.els.save.disabled).toBe(false);
    await doc.els.save._click();
    const saveCall = calls.find((c) => c.url.endsWith("/api/rules") && c.opts.method === "POST");
    expect(JSON.parse(saveCall.opts.body)).toEqual({
      pattern: "borneo.lc",
      instance_id: "prox",
    });
    expect(doc.els.status.textContent).toBe("Rule saved.");
  });

  // --- the §8 confirm gate is TWO deliberate actions --------------------------
  it("a 409 shows the server's impact and only a SECOND click confirms it", async () => {
    const doc = fakeDoc();
    const chromeApi = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p },
      tabs: { query: async () => [{ id: 1, url: "https://borneo.lc/dashboard" }] },
    };
    const { fetchFn, calls } = routedFetch({ relocations: 3, closures: 1 }, (n) =>
      n === 1
        ? {
            status: 409,
            json: async () => ({
              relocations: 3,
              closures: 1,
              requires_confirm: true,
              not_counted: [{ id: "old" }],
              error: "confirm_impact required",
            }),
          }
        : { status: 201, json: async () => ({ ok: true, id: 9 }) },
    );

    await init(doc, chromeApi, fetchFn);
    await doc.els.save._click(); // 1st click: unconfirmed

    const saves = () => calls.filter((c) => c.url.endsWith("/api/rules") && c.opts.method === "POST");
    expect(JSON.parse(saves()[0].opts.body).confirm_impact).toBeUndefined();
    // The server's impact is rendered and the button is re-armed for a second act.
    expect(doc.els.impact.textContent).toContain("relocate 3");
    expect(doc.els.impact.textContent).toContain("old"); // not-counted instance named
    expect(doc.els.save.disabled).toBe(false);
    expect(doc.els.save.textContent).toBe("Confirm and save");
    expect(doc.els.status.textContent).toContain("click again to confirm");

    await doc.els.save._click(); // 2nd click: the human confirms what was shown
    expect(JSON.parse(saves()[1].opts.body).confirm_impact).toBe(true);
    expect(doc.els.status.textContent).toBe("Rule saved.");
  });

  it("a 409 whose body cannot be read does NOT arm the confirm (no zeros, no blind ok)", async () => {
    // An unreadable 409 used to render "relocate 0, close 0" — zeros where the server
    // refused BECAUSE the impact is non-zero — and arm a confirm for an impact nobody
    // saw. The next click would then send confirm_impact:true blind.
    const doc = fakeDoc();
    const chromeApi = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p },
      tabs: { query: async () => [{ id: 1, url: "https://borneo.lc/x" }] },
    };
    const { fetchFn, calls } = routedFetch({ relocations: 3, closures: 1 }, () => ({
      status: 409,
      json: async () => {
        throw new Error("not json");
      },
    }));

    await init(doc, chromeApi, fetchFn);
    await doc.els.save._click();
    expect(doc.els.impact.textContent).not.toContain("relocate 0");
    expect(doc.els.status.textContent).toContain("could not be read");

    // A second click must STILL be unconfirmed — nothing was ever shown.
    await doc.els.save._click();
    const saves = calls.filter((c) => c.url.endsWith("/api/rules") && c.opts.method === "POST");
    expect(saves.every((c) => JSON.parse(c.opts.body).confirm_impact === undefined)).toBe(true);
  });

  it("re-enables Save after a retryable failure (423 paused / 422 / 5xx)", async () => {
    // A paused curator answers 423 and resumes later; a typo gets fixed. Leaving the
    // button disabled forced the human to close and reopen the popup.
    const doc = fakeDoc();
    const chromeApi = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p },
      tabs: { query: async () => [{ id: 1, url: "https://borneo.lc/x" }] },
    };
    const { fetchFn } = routedFetch({ relocations: 0, closures: 0 }, (n) =>
      n === 1
        ? { status: 423, json: async () => ({ error: "paused", until: 1 }) }
        : { status: 201, json: async () => ({ ok: true, id: 3 }) },
    );

    await init(doc, chromeApi, fetchFn);
    await doc.els.save._click();
    expect(doc.els.status.textContent).toContain("423");
    expect(doc.els.save.disabled).toBe(false); // retryable
    expect(doc.els.save.textContent).toBe("Save rule"); // not armed as a confirm

    await doc.els.save._click();
    expect(doc.els.status.textContent).toBe("Rule saved.");
    expect(doc.els.save.disabled).toBe(true); // saved: no duplicate rule on a re-click
  });

  it("a thrown save disarms BOTH the flag and the button label", async () => {
    const doc = fakeDoc();
    const chromeApi = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p },
      tabs: { query: async () => [{ id: 1, url: "https://borneo.lc/x" }] },
    };
    let n = 0;
    const { fetchFn, calls } = routedFetch({ relocations: 2, closures: 0 }, () => {
      n += 1;
      if (n === 1) return { status: 409, json: async () => ({ relocations: 2, closures: 0 }) };
      throw new Error("network down");
    });

    await init(doc, chromeApi, fetchFn);
    await doc.els.save._click(); // armed by the 409
    expect(doc.els.save.textContent).toBe("Confirm and save");

    await doc.els.save._click(); // throws
    // The label must not keep promising a confirmation the next click will not send.
    expect(doc.els.save.textContent).toBe("Save rule");
    await doc.els.save._click();
    const saves = calls.filter((c) => c.url.endsWith("/api/rules") && c.opts.method === "POST");
    expect(JSON.parse(saves[saves.length - 1].opts.body).confirm_impact).toBeUndefined();
  });

  it("a FAILED preview never auto-confirms: save goes unconfirmed and the gate speaks", async () => {
    // "Preview unavailable" used to still send confirm_impact:true — confirming an
    // impact the human was never shown. Now the first save is unconfirmed and the
    // server's 409 is what surfaces the impact.
    const doc = fakeDoc();
    const chromeApi = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p },
      tabs: { query: async () => [{ id: 1, url: "https://borneo.lc/dashboard" }] },
    };
    const { fetchFn, calls } = routedFetch(new Error("preview down"), (n) =>
      n === 1
        ? { status: 409, json: async () => ({ relocations: 7, closures: 0, requires_confirm: true }) }
        : { status: 201, json: async () => ({ ok: true, id: 9 }) },
    );

    await init(doc, chromeApi, fetchFn);
    expect(doc.els.impact.textContent).toContain("Preview unavailable");

    await doc.els.save._click();
    const saves = () => calls.filter((c) => c.url.endsWith("/api/rules") && c.opts.method === "POST");
    expect(JSON.parse(saves()[0].opts.body).confirm_impact).toBeUndefined();
    expect(doc.els.impact.textContent).toContain("relocate 7"); // impact finally shown

    await doc.els.save._click();
    expect(JSON.parse(saves()[1].opts.body).confirm_impact).toBe(true);
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

// --- §7: the popup prefers the SW credential (raw secret Bearer) over instance.json ---
describe("loadPopupContext (§7)", () => {
  it("uses get_credential + get_identity from the SW (the RAW secret is the /api Bearer)", async () => {
    const { loadPopupContext } = await import("../pages/popup.js");
    const chromeApi = {
      runtime: {
        getURL: (p) => "chrome-extension://mock/" + p,
        sendMessage: async (msg) => {
          if (msg.type === "get_credential") return { serviceUrl: "wss://curator/", secret: "raw-abc" };
          if (msg.type === "get_identity") return { instanceId: "srv-7" };
          return null;
        },
      },
    };
    // fetch must NOT be consulted when the SW answers.
    const fetchFn = vi.fn();
    const ctx = await loadPopupContext(chromeApi, fetchFn);
    expect(ctx).toEqual({ base: "https://curator", token: "raw-abc", instanceId: "srv-7" });
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("falls back to instance.json when the SW has no credential yet", async () => {
    const { loadPopupContext } = await import("../pages/popup.js");
    const chromeApi = {
      runtime: {
        getURL: (p) => "chrome-extension://mock/" + p,
        sendMessage: async () => null, // SW channel empty (pre-enrollment)
      },
    };
    const fetchFn = vi.fn(async () => ({ json: async () => ({ serviceUrl: "wss://host.example/", token: "tok", instanceId: "prox" }) }));
    const ctx = await loadPopupContext(chromeApi, fetchFn);
    expect(ctx).toEqual({ base: "https://host.example", token: "tok", instanceId: "prox" });
    expect(fetchFn).toHaveBeenCalled();
  });
});
