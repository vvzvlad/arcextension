import { describe, it, expect, vi, afterEach } from "vitest";
import {
  httpBaseFromServiceUrl,
  buildRule,
  confirmHeadline,
  instanceChoices,
  requestPreview,
  saveRule,
  summarize,
  init,
  PATTERN_DEBOUNCE_MS,
} from "../pages/popup.js";

// The popup's context comes from the SERVICE WORKER only (§7): the validated address,
// the RAW instance secret as the /api Bearer, and the SERVER-assigned id. There is no
// instance.json credential anywhere in the model, so the fake SW is the whole source.
const SW_RUNTIME = {
  getURL: (p) => "chrome-extension://mock/" + p,
  sendMessage: async (msg) => {
    if (msg.type === "get_credential") {
      return { serviceUrl: "wss://host.example/", secret: "the-secret" };
    }
    if (msg.type === "get_identity") return { instanceId: "prox", title: "Prox" };
    return null;
  },
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

describe("instanceChoices", () => {
  it("keeps the server order and marks the browser the popup runs in", () => {
    // The label is the bare id: the id IS the name (§6), so the old "Title (id)" pairing
    // has nothing left to pair. A stray `title` on the server row must not resurrect it.
    const choices = instanceChoices(
      [{ id: "main", title: "Curator Main" }, { id: "prox" }],
      "prox",
    );
    expect(choices.map((c) => c.id)).toEqual(["main", "prox"]);
    expect(choices[0].label).toBe("main");
    expect(choices[1].label).toContain("this browser");
  });

  it("ALWAYS offers the current instance, even when /api/state does not list it", () => {
    // /api/state carries only ACTIVE instances (src/db/state.py). Targeting this browser
    // is what the popup did unconditionally before the target became pickable, so it
    // must never become unreachable — otherwise a copy whose row is not active yet
    // cannot file the rule it was opened for at all.
    const choices = instanceChoices([{ id: "main" }], "prox");
    expect(choices[0].id).toBe("prox");
    expect(choices.map((c) => c.id)).toEqual(["prox", "main"]);
    expect(instanceChoices([], "prox").map((c) => c.id)).toEqual(["prox"]);
    expect(instanceChoices(null, "prox").map((c) => c.id)).toEqual(["prox"]);
  });
});

// --- what the impact block SAYS (§8's three confirm grounds) ------------------
describe("summarize / confirmHeadline: the gate names its ground", () => {
  // `_requires_confirm` (src/api/rules.py) fires on THREE independent grounds and only
  // one of them is a number. Reporting only the numbers produced the screen the owner
  // sent: "Would relocate 0 tab(s) and close 0 tab(s)" beside an armed "Confirm and
  // save" and a status claiming the rule "has an impact".
  it("names the FIRST-RULE ground when the counts are zero (the reported bug)", () => {
    const body = {
      relocations: 0,
      closures: 0,
      enables_drain: true,
      disables_curation: false,
      not_counted: [],
    };
    const text = summarize(body, { requiresConfirm: true });
    expect(text).toContain("FIRST active rule");
    expect(text).toContain("main browser"); // WHAT it turns on: the fleet-wide drain
    // The status row must not claim an impact it just printed as zero.
    expect(confirmHeadline(body)).not.toMatch(/has an impact/i);
    expect(confirmHeadline(body)).toContain("first active rule");
  });

  it("names the UNCOUNTED ground when the counts are zero because nobody answered", () => {
    const body = {
      relocations: 0,
      closures: 0,
      enables_drain: true,
      not_counted: [{ id: "old", reason: "disconnected" }],
    };
    const text = summarize(body, { requiresConfirm: true });
    expect(text).toContain("old");
    expect(text).toContain("can be larger");
    // …and NOT the first-rule story: an uncounted instance is a different ground and
    // the elimination that identifies the boundary crossing does not apply.
    expect(text).not.toContain("FIRST active rule");
    expect(confirmHeadline(body)).toContain("could not be counted");
  });

  it("names the numbers when there ARE numbers", () => {
    const body = { relocations: 3, closures: 1, enables_drain: true, not_counted: [] };
    expect(confirmHeadline(body)).toContain("3 tab(s) would move");
    expect(summarize(body, { requiresConfirm: true })).toContain("relocate 3");
  });

  it("says a zero means 'the next pass', not 'the rule matches nothing'", () => {
    // preview.py `_guarded`: a tab that is not idle long enough (or is pinned / audible
    // / on screen) is NOT counted. The owner read a zero as "the rule does not work".
    const text = summarize({ relocations: 0, closures: 0 });
    expect(text).toContain("NEXT pass");
    expect(text).toMatch(/recently/);
  });

  it("names the curation-off ground for a rule set that ends up empty", () => {
    const body = { relocations: 0, closures: 0, enables_drain: false, disables_curation: true };
    expect(summarize(body, { requiresConfirm: true })).toContain("curation stops completely");
    expect(confirmHeadline(body)).toContain("turns curation off");
  });
});

// --- network helpers use the server matcher (§8) ----------------------------
function jsonResp(status, body) {
  // A real Response carries TEXT; the popup parses it itself so a 409 (JSON) and a 422
  // (Starlette PlainTextResponse) can both be read from one consumption of the body.
  return { status, text: async () => JSON.stringify(body) };
}

function textResp(status, text) {
  return { status, text: async () => text };
}

describe("requestPreview / saveRule", () => {
  it("previews with a Bearer token and the host pattern body", async () => {
    const fetchFn = vi.fn(async () => jsonResp(200, { relocations: 2, closures: 0 }));
    const rule = { pattern: "borneo.lc", instance_id: "prox" };
    const payload = await requestPreview(fetchFn, "https://host.example", "tok", rule);
    expect(payload).toEqual({ relocations: 2, closures: 0 });
    const [url, opts] = fetchFn.mock.calls[0];
    expect(url).toBe("https://host.example/api/rules/preview");
    expect(opts.method).toBe("POST");
    expect(opts.headers.Authorization).toBe("Bearer tok");
    expect(JSON.parse(opts.body)).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
  });

  it("throws the SERVER's words on a 422 (validation is the server's job, §8)", async () => {
    const fetchFn = vi.fn(async () =>
      textResp(422, "invalid rule pattern: pattern looks like a URL (contains '/')"),
    );
    await expect(
      requestPreview(fetchFn, "https://host.example", "tok", { pattern: "https://x/y" }),
    ).rejects.toThrow(/looks like a URL/);
  });

  it("saves WITHOUT confirm_impact by default (the §8 gate is not echo-confirmed)", async () => {
    const fetchFn = vi.fn(async () => jsonResp(201, { ok: true, id: 7 }));
    const rule = { pattern: "borneo.lc", instance_id: "prox" };
    const res = await saveRule(fetchFn, "https://host.example", "tok", rule);
    expect(res.status).toBe(201);
    const [url, opts] = fetchFn.mock.calls[0];
    expect(url).toBe("https://host.example/api/rules");
    // No confirm_impact key at all: the human has confirmed nothing yet.
    expect(JSON.parse(opts.body)).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
  });

  it("sends confirm_impact:true only when explicitly asked", async () => {
    const fetchFn = vi.fn(async () => jsonResp(201, { ok: true, id: 7 }));
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
function fakeEl(tag = "div") {
  return {
    tag,
    textContent: "",
    value: "",
    disabled: false,
    children: [],
    _handlers: {},
    addEventListener(type, fn) {
      this._handlers[type] = fn;
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    // Test drivers: return whatever the handler returns so an async one can be awaited.
    _click() {
      return this._handlers.click && this._handlers.click();
    },
    _type(v) {
      this.value = v;
      return this._handlers.input && this._handlers.input();
    },
    _pick(v) {
      this.value = v;
      return this._handlers.change && this._handlers.change();
    },
  };
}

function fakeDoc() {
  const els = {};
  for (const id of ["origin", "pattern", "target", "impact", "save", "status"]) {
    els[id] = fakeEl();
  }
  return { els, getElementById: (id) => els[id], createElement: (tag) => fakeEl(tag) };
}

const FLEET = { instances: [{ id: "main", title: "Main" }, { id: "prox", title: "Prox" }] };

// `preview` may be an Error (the call fails), a payload, or a function of
// (requestBody, callIndex) returning a response. `save` is a function of the call index
// so a test can answer 409-then-201, mirroring the §8 confirm gate. `state` may be an
// Error so a test can take the fleet list away.
function routedFetch(preview, save, state) {
  const calls = [];
  let saveCalls = 0;
  let previewCalls = 0;
  const fetchFn = vi.fn(async (url, opts) => {
    calls.push({ url, opts });
    if (url.endsWith("instance.json")) {
      // The popup must never read instance.json again: it carries no token and no id.
      throw new Error("instance.json must not be fetched by the popup");
    }
    if (url.endsWith("/api/state")) {
      if (state instanceof Error) throw state;
      return jsonResp(200, state || FLEET);
    }
    if (url.endsWith("/api/rules/preview")) {
      previewCalls += 1;
      if (preview instanceof Error) throw preview;
      if (typeof preview === "function") return preview(JSON.parse(opts.body), previewCalls);
      return jsonResp(200, preview);
    }
    if (url.endsWith("/api/rules")) {
      saveCalls += 1;
      if (save) return save(saveCalls, JSON.parse(opts.body));
      return jsonResp(201, { ok: true, id: 9 });
    }
    throw new Error("unexpected fetch " + url);
  });
  return { fetchFn, calls };
}

function chromeOn(url) {
  return { runtime: SW_RUNTIME, tabs: { query: async () => [{ id: 1, url }] } };
}

const previews = (calls) => calls.filter((c) => c.url.endsWith("/api/rules/preview"));
const saves = (calls) =>
  calls.filter((c) => c.url.endsWith("/api/rules") && c.opts.method === "POST");
const bodyOf = (call) => JSON.parse(call.opts.body);

afterEach(() => {
  vi.useRealTimers();
});

describe("init: build -> preview -> save", () => {
  it("builds the <origin>/* rule for THIS instance, previews it, and saves on click", async () => {
    const doc = fakeDoc();
    const preview = {
      relocations: 3,
      closures: 1,
      instances: [
        { id: "prox", counted: true },
        { id: "old", counted: false }, // freshness surfaced to the human
      ],
    };
    const { fetchFn, calls } = routedFetch(preview);

    await init(doc, chromeOn("https://borneo.lc/dashboard"), fetchFn);

    // Built the rule for the current instance and rendered it into the EDITABLE fields.
    expect(doc.els.origin.textContent).toBe("https://borneo.lc/*");
    expect(doc.els.pattern.value).toBe("borneo.lc");
    expect(doc.els.target.value).toBe("prox");
    // Previewed via the server matcher; impact + "not counted" surfaced.
    expect(bodyOf(previews(calls)[0])).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
    expect(doc.els.impact.textContent).toContain("relocate 3");
    expect(doc.els.impact.textContent).toContain("close 1");
    expect(doc.els.impact.textContent).toContain("old"); // not-counted instance named

    // Save is enabled; the FIRST click posts WITHOUT confirm_impact (§8: the gate is
    // the server's question, and one click must not answer it blind).
    expect(doc.els.save.disabled).toBe(false);
    await doc.els.save._click();
    expect(bodyOf(saves(calls)[0])).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
    expect(doc.els.status.textContent).toContain("Rule saved.");
  });

  // --- the target browser is PICKED, not assumed ------------------------------
  it("offers the fleet from /api/state and defaults to the browser the popup is in", async () => {
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch({ relocations: 0, closures: 0 });

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);

    const stateCall = calls.find((c) => c.url.endsWith("/api/state"));
    expect(stateCall.opts.headers.Authorization).toBe("Bearer the-secret");
    expect(doc.els.target.children.map((o) => o.value)).toEqual(["main", "prox"]);
    // Ids are SERVER strings: textContent, never innerHTML.
    expect(doc.els.target.children[0].textContent).toBe("main");
    expect(doc.els.target.value).toBe("prox"); // the current instance stays the default
  });

  it("still files a rule for THIS browser when the fleet list cannot be read", async () => {
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch(
      { relocations: 0, closures: 0 },
      null,
      new Error("network down"),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);

    expect(doc.els.status.textContent).toContain("Could not load the list of browsers");
    expect(doc.els.target.children.map((o) => o.value)).toEqual(["prox"]);
    expect(doc.els.save.disabled).toBe(false); // the popup still works
    await doc.els.save._click();
    expect(bodyOf(saves(calls)[0])).toEqual({ pattern: "borneo.lc", instance_id: "prox" });
  });

  it("recomputes the preview for the new destination when the target changes", async () => {
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch((body) =>
      jsonResp(200, { relocations: body.instance_id === "main" ? 12 : 1, closures: 0 }),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    expect(doc.els.impact.textContent).toContain("relocate 1");

    await doc.els.target._pick("main"); // picking is one deliberate act: no debounce
    expect(previews(calls)).toHaveLength(2);
    expect(bodyOf(previews(calls)[1])).toEqual({ pattern: "borneo.lc", instance_id: "main" });
    expect(doc.els.impact.textContent).toContain("relocate 12");
  });

  it("recomputes the preview for an edited pattern, once per burst of keystrokes", async () => {
    vi.useFakeTimers();
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch((body) =>
      jsonResp(200, { relocations: body.pattern === "*.borneo.lc" ? 9 : 1, closures: 0 }),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    expect(previews(calls)).toHaveLength(1);

    // Three keystrokes in one burst: the preview is a WHOLE-PASS simulation that pokes
    // every instance for a fresh snapshot, so it must not run per character.
    doc.els.pattern._type("*.borneo");
    doc.els.pattern._type("*.borneo.l");
    doc.els.pattern._type("*.borneo.lc");
    expect(previews(calls)).toHaveLength(1); // nothing sent yet

    await vi.advanceTimersByTimeAsync(PATTERN_DEBOUNCE_MS + 5);
    expect(previews(calls)).toHaveLength(2); // exactly ONE for the whole burst
    expect(bodyOf(previews(calls)[1])).toEqual({
      pattern: "*.borneo.lc",
      instance_id: "prox",
    });
    expect(doc.els.impact.textContent).toContain("relocate 9");
  });

  // --- THE one: an edit must take the armed confirmation with it --------------
  it("editing the PATTERN after a 409 disarms the confirm (no blind confirmation)", async () => {
    // The bypass this prevents: see the impact of `borneo.lc` (2/0), widen the pattern to
    // `*.lc` (300/120) and click the still-armed "Confirm and save" — confirm_impact goes
    // out for a rule whose impact the server never showed. That is the echo-confirmation
    // the §8 two-step exists to stop.
    vi.useFakeTimers();
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch(
      (body) => jsonResp(200, { relocations: body.pattern === "*.lc" ? 300 : 2, closures: 0 }),
      (n, body) =>
        body.confirm_impact
          ? jsonResp(201, { ok: true, id: 1 })
          : jsonResp(409, {
              relocations: body.pattern === "*.lc" ? 300 : 2,
              closures: 0,
              requires_confirm: true,
            }),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    await doc.els.save._click(); // 1st click: unconfirmed probe → 409 arms
    expect(doc.els.save.textContent).toBe("Confirm and save");
    expect(doc.els.impact.textContent).toContain("relocate 2");

    doc.els.pattern._type("*.lc"); // the human widens the rule instead of confirming
    // Disarmed IMMEDIATELY — not only once the new preview lands — and the button stops
    // claiming the numbers next to it are the numbers of what it would save.
    expect(doc.els.save.textContent).not.toBe("Confirm and save");
    expect(doc.els.save.disabled).toBe(true);
    expect(doc.els.impact.textContent).not.toContain("relocate 2");

    await vi.advanceTimersByTimeAsync(PATTERN_DEBOUNCE_MS + 5);
    expect(doc.els.impact.textContent).toContain("relocate 300"); // the NEW rule's impact
    expect(doc.els.save.disabled).toBe(false);

    // The next click is a fresh PROBE for the new rule, never a confirmation of the old.
    await doc.els.save._click();
    expect(bodyOf(saves(calls)[1])).toMatchObject({ pattern: "*.lc" });
    expect(bodyOf(saves(calls)[1]).confirm_impact).toBeUndefined();
  });

  it("changing the TARGET after a 409 disarms the confirm too (tabs would go elsewhere)", async () => {
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch({ relocations: 5, closures: 0 }, (n, body) =>
      body.confirm_impact
        ? jsonResp(201, { ok: true, id: 1 })
        : jsonResp(409, { relocations: 5, closures: 0, requires_confirm: true }),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    await doc.els.save._click();
    expect(doc.els.save.textContent).toBe("Confirm and save");

    await doc.els.target._pick("main"); // same pattern, DIFFERENT destination
    expect(doc.els.save.textContent).toBe("Save rule");

    await doc.els.save._click();
    expect(bodyOf(saves(calls)[1])).toMatchObject({ instance_id: "main" });
    expect(bodyOf(saves(calls)[1]).confirm_impact).toBeUndefined();
  });

  it("an edit made WHILE the save is in flight cannot be re-armed by the arriving 409", async () => {
    // The 409 handler arms AFTER its await. An edit inside that window is disarmed by
    // the edit and then re-armed by the answer — and the next click confirms a rule the
    // server never previewed. Same guard (and same reason) as the startpage's
    // `draftRevision` snapshot in App.vue onSave.
    vi.useFakeTimers();
    const doc = fakeDoc();
    let release;
    const parked = new Promise((r) => (release = r));
    const { fetchFn, calls } = routedFetch({ relocations: 2, closures: 0 }, async (n, body) => {
      if (n === 1) await parked;
      return body.confirm_impact
        ? jsonResp(201, { ok: true, id: 1 })
        : jsonResp(409, { relocations: 2, closures: 0, requires_confirm: true });
    });

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    const firstClick = doc.els.save._click(); // parked in flight

    doc.els.pattern._type("*.lc"); // edited while the save is in flight
    await vi.advanceTimersByTimeAsync(PATTERN_DEBOUNCE_MS + 5);

    release();
    await firstClick;

    // The 409 for `borneo.lc` must NOT arm a confirm for `*.lc`.
    expect(doc.els.save.textContent).not.toBe("Confirm and save");
    await doc.els.save._click();
    const last = saves(calls)[saves(calls).length - 1];
    expect(bodyOf(last)).toMatchObject({ pattern: "*.lc" });
    expect(bodyOf(last).confirm_impact).toBeUndefined();
  });

  it("a STALE preview answer never overwrites the fresh one", async () => {
    // Two edits, answers in reverse order. Without a request generation the slow first
    // answer lands last and puts the impact of a rule the human has left on the screen.
    vi.useFakeTimers();
    const doc = fakeDoc();
    const deferred = {};
    const { fetchFn, calls } = routedFetch((body) => {
      if (body.pattern === "borneo.lc") return jsonResp(200, { relocations: 1, closures: 0 });
      return new Promise((resolve) => {
        deferred[body.pattern] = () =>
          resolve(jsonResp(200, { relocations: body.pattern === "a.lc" ? 99 : 2, closures: 0 }));
      });
    });

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);

    doc.els.pattern._type("a.lc");
    await vi.advanceTimersByTimeAsync(PATTERN_DEBOUNCE_MS + 5);
    doc.els.pattern._type("b.lc");
    await vi.advanceTimersByTimeAsync(PATTERN_DEBOUNCE_MS + 5);
    expect(previews(calls)).toHaveLength(3);

    deferred["b.lc"](); // the CURRENT rule answers first…
    await vi.advanceTimersByTimeAsync(1);
    expect(doc.els.impact.textContent).toContain("relocate 2");

    deferred["a.lc"](); // …and the abandoned one answers late
    await vi.advanceTimersByTimeAsync(1);
    expect(doc.els.impact.textContent).toContain("relocate 2"); // still the fresh one
    expect(doc.els.impact.textContent).not.toContain("relocate 99");
  });

  // --- the §8 confirm gate is TWO deliberate actions --------------------------
  it("a 409 shows the server's impact and only a SECOND click confirms it", async () => {
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch({ relocations: 3, closures: 1 }, (n) =>
      n === 1
        ? jsonResp(409, {
            relocations: 3,
            closures: 1,
            requires_confirm: true,
            not_counted: [{ id: "old" }],
            error: "confirm_impact required",
          })
        : jsonResp(201, { ok: true, id: 9 }),
    );

    await init(doc, chromeOn("https://borneo.lc/dashboard"), fetchFn);
    await doc.els.save._click(); // 1st click: unconfirmed

    expect(bodyOf(saves(calls)[0]).confirm_impact).toBeUndefined();
    // The server's impact is rendered and the button is re-armed for a second act.
    expect(doc.els.impact.textContent).toContain("relocate 3");
    expect(doc.els.impact.textContent).toContain("old"); // not-counted instance named
    expect(doc.els.save.disabled).toBe(false);
    expect(doc.els.save.textContent).toBe("Confirm and save");
    expect(doc.els.status.textContent).toContain("click again to confirm");

    await doc.els.save._click(); // 2nd click: the human confirms what was shown
    expect(bodyOf(saves(calls)[1]).confirm_impact).toBe(true);
    expect(doc.els.status.textContent).toContain("Rule saved.");
  });

  it("a zero-impact 409 explains WHY it is being gated instead of showing bare zeros", async () => {
    // The owner's screenshot: 0/0, an armed button, and "this rule has an impact". The
    // real ground was the empty→non-empty boundary — the first rule switching the
    // fleet-wide drain on — which is far bigger than any tab count.
    const doc = fakeDoc();
    const { fetchFn } = routedFetch({ relocations: 0, closures: 0 }, () =>
      jsonResp(409, {
        relocations: 0,
        closures: 0,
        impact: 0,
        requires_confirm: true,
        not_counted: [],
        enables_drain: true,
        disables_curation: false,
        error: "confirm_impact required",
      }),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    await doc.els.save._click();

    expect(doc.els.save.textContent).toBe("Confirm and save"); // armed, as the server asked
    expect(doc.els.impact.textContent).toContain("FIRST active rule");
    expect(doc.els.impact.textContent).toContain("main browser");
    expect(doc.els.status.textContent).not.toMatch(/has an impact/i);
    expect(doc.els.status.textContent).toContain("first active rule");
  });

  it("a 409 whose body cannot be read does NOT arm the confirm (no zeros, no blind ok)", async () => {
    // An unreadable 409 used to render "relocate 0, close 0" — zeros where the server
    // refused BECAUSE the impact is non-zero — and arm a confirm for an impact nobody
    // saw. The next click would then send confirm_impact:true blind.
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch({ relocations: 3, closures: 1 }, () =>
      textResp(409, "<html>502 from a proxy</html>"),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    await doc.els.save._click();
    expect(doc.els.impact.textContent).not.toContain("relocate 0");
    expect(doc.els.save.textContent).not.toBe("Confirm and save");
    expect(doc.els.status.textContent).toContain("could not be read");

    // A second click must STILL be unconfirmed — nothing was ever shown.
    await doc.els.save._click();
    expect(saves(calls).every((c) => bodyOf(c).confirm_impact === undefined)).toBe(true);
  });

  it("re-enables Save after a retryable failure (423 paused / 422 / 5xx)", async () => {
    // A paused curator answers 423 and resumes later; a typo gets fixed. Leaving the
    // button disabled forced the human to close and reopen the popup.
    const doc = fakeDoc();
    const { fetchFn } = routedFetch({ relocations: 0, closures: 0 }, (n) =>
      n === 1
        ? jsonResp(423, { error: "paused", until: 1 })
        : jsonResp(201, { ok: true, id: 3 }),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    await doc.els.save._click();
    expect(doc.els.status.textContent).toContain("423");
    // …and it says what to DO about it, not only that it failed.
    expect(doc.els.status.textContent).toContain("paused");
    expect(doc.els.status.textContent).toContain("start page");
    expect(doc.els.save.disabled).toBe(false); // retryable
    expect(doc.els.save.textContent).toBe("Save rule"); // not armed as a confirm

    await doc.els.save._click();
    expect(doc.els.status.textContent).toContain("Rule saved.");
    expect(doc.els.save.disabled).toBe(true); // saved: no duplicate rule on a re-click
  });

  it("a 422 for a bad pattern is shown in the SERVER's words and stays retryable", async () => {
    // Validation lives on the server (§8 — a browser-side copy of the matcher "would lie
    // on IDN and IPv6"), so the popup's whole job is to relay what it said, twice: once
    // when the preview refuses the pattern and once if the human saves it anyway.
    vi.useFakeTimers();
    const doc = fakeDoc();
    const detail =
      "invalid rule pattern: pattern looks like a URL (contains '/'); " +
      "enter only a host pattern like 'borneo.lc' or 'borneo.lc:8443'";
    const { fetchFn } = routedFetch(
      (body) =>
        body.pattern.includes("/")
          ? textResp(422, detail)
          : jsonResp(200, { relocations: 0, closures: 0 }),
      (n, body) => (body.pattern.includes("/") ? textResp(422, detail) : jsonResp(201, { ok: true })),
    );

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);

    doc.els.pattern._type("https://borneo.lc/path");
    await vi.advanceTimersByTimeAsync(PATTERN_DEBOUNCE_MS + 5);
    expect(doc.els.impact.textContent).toContain("looks like a URL");

    await doc.els.save._click();
    expect(doc.els.status.textContent).toContain("422");
    expect(doc.els.status.textContent).toContain("looks like a URL");
    expect(doc.els.save.disabled).toBe(false); // a typo is fixable in place

    // Fixing it recovers without reopening the popup.
    doc.els.pattern._type("borneo.lc");
    await vi.advanceTimersByTimeAsync(PATTERN_DEBOUNCE_MS + 5);
    await doc.els.save._click();
    expect(doc.els.status.textContent).toContain("Rule saved.");
  });

  it("a thrown save disarms BOTH the flag and the button label", async () => {
    const doc = fakeDoc();
    let n = 0;
    const { fetchFn, calls } = routedFetch({ relocations: 2, closures: 0 }, () => {
      n += 1;
      if (n === 1) return jsonResp(409, { relocations: 2, closures: 0 });
      throw new Error("network down");
    });

    await init(doc, chromeOn("https://borneo.lc/x"), fetchFn);
    await doc.els.save._click(); // armed by the 409
    expect(doc.els.save.textContent).toBe("Confirm and save");

    await doc.els.save._click(); // throws
    // The label must not keep promising a confirmation the next click will not send.
    expect(doc.els.save.textContent).toBe("Save rule");
    await doc.els.save._click();
    const last = saves(calls)[saves(calls).length - 1];
    expect(bodyOf(last).confirm_impact).toBeUndefined();
  });

  it("a FAILED preview never auto-confirms: save goes unconfirmed and the gate speaks", async () => {
    // "Preview unavailable" used to still send confirm_impact:true — confirming an
    // impact the human was never shown. Now the first save is unconfirmed and the
    // server's 409 is what surfaces the impact.
    const doc = fakeDoc();
    const { fetchFn, calls } = routedFetch(new Error("preview down"), (n) =>
      n === 1
        ? jsonResp(409, { relocations: 7, closures: 0, requires_confirm: true })
        : jsonResp(201, { ok: true, id: 9 }),
    );

    await init(doc, chromeOn("https://borneo.lc/dashboard"), fetchFn);
    expect(doc.els.impact.textContent).toContain("Preview unavailable");
    // The screen says what this means for the human: saving is still safe, because the
    // server recomputes and asks. Otherwise "unavailable" reads as "do not touch this".
    expect(doc.els.impact.textContent).toContain("You can still save");
    expect(doc.els.save.disabled).toBe(false); // a broken preview must not block saving

    await doc.els.save._click();
    expect(bodyOf(saves(calls)[0]).confirm_impact).toBeUndefined();
    expect(doc.els.impact.textContent).toContain("relocate 7"); // impact finally shown

    await doc.els.save._click();
    expect(bodyOf(saves(calls)[1]).confirm_impact).toBe(true);
  });

  it("disables save when the tab cannot become a rule (non-http)", async () => {
    const doc = fakeDoc();
    const { fetchFn } = routedFetch({ relocations: 0, closures: 0 });
    await init(doc, chromeOn("chrome://settings"), fetchFn);
    expect(doc.els.save.disabled).toBe(true);
    expect(doc.els.status.textContent).toContain("Cannot build a rule");
  });
});

// --- §7: the SW is the ONLY credential source; there is no instance.json fallback ----
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
    const ctx = await loadPopupContext(chromeApi);
    expect(ctx).toEqual({ base: "https://curator", token: "raw-abc", instanceId: "srv-7" });
  });

  it("REFUSES (with a reason) instead of falling back to instance.json fields that no longer exist", async () => {
    // The old fallback read `config.token` / `config.instanceId`. Neither field exists in
    // any bundle under enrollment, so it could only build `{token: undefined,
    // instanceId: undefined}` — a rule targeted at `undefined`, saved with no credential,
    // i.e. a guaranteed 401 presented to the human as a working popup. Reverting to a
    // fallback reddens this.
    const { loadPopupContext } = await import("../pages/popup.js");
    const chromeApi = {
      runtime: {
        getURL: (p) => "chrome-extension://mock/" + p,
        sendMessage: async () => null, // SW channel empty (pre-enrollment)
      },
    };
    await expect(loadPopupContext(chromeApi)).rejects.toThrow(/service address/i);
  });

  it("names the not-enrolled-yet case when the address is set but there is no secret/id", async () => {
    const { loadPopupContext } = await import("../pages/popup.js");
    const chromeApi = {
      runtime: {
        getURL: (p) => "chrome-extension://mock/" + p,
        sendMessage: async (msg) =>
          msg.type === "get_credential" ? { serviceUrl: "wss://curator/", secret: null } : null,
      },
    };
    await expect(loadPopupContext(chromeApi)).rejects.toThrow(/not enrolled/i);
  });

  it("tells a REFUSED address apart from an absent one", async () => {
    // The TLS gate resolves a refused address to null, exactly like an unset one, so the
    // popup used to tell an operator who had typed `ws://host` that no address was
    // configured — over a field they had filled in. `addressError` is what separates the
    // two, and this is the third surface to read it (options + startpage already do).
    // Reddens if get_credential stops carrying it or the popup stops branching on it.
    const { loadPopupContext } = await import("../pages/popup.js");
    const chromeApi = {
      runtime: {
        getURL: (p) => "chrome-extension://mock/" + p,
        sendMessage: async (msg) =>
          msg.type === "get_credential"
            ? { serviceUrl: null, addressError: "insecure", secret: "raw-abc" }
            : null,
      },
    };
    await expect(loadPopupContext(chromeApi)).rejects.toThrow(/refused/i);
    await expect(loadPopupContext(chromeApi)).rejects.toThrow(/insecure/);
    // …and the ABSENT case still says "not configured", not "refused".
    const unset = {
      runtime: {
        getURL: (p) => "chrome-extension://mock/" + p,
        sendMessage: async (msg) =>
          msg.type === "get_credential"
            ? { serviceUrl: null, addressError: null, secret: null }
            : null,
      },
    };
    await expect(loadPopupContext(unset)).rejects.toThrow(/no service address is configured/i);
  });
});
