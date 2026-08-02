import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createChromeMock, FakeWebSocket } from "./chrome-mock.js";
import { RECONNECT_ALARM, TICK_ALARM, CONNECTION_STATE_KEY } from "../src/constants.js";
import { QUEUE_KEY } from "../src/quicklinks.js";

// The service worker is an ENTRY module: importing it registers the chrome listeners
// and runs its start-up work (alarms, socket, stranded-flush). So every test imports
// it FRESH (vi.resetModules) against a fresh chrome mock + globals.

const CONFIG = { instanceId: "prox", title: "Prox", serviceUrl: "wss://host.example/", token: "tok" };

// Let the module's top-level async work settle. The storage mock resolves on real
// macrotasks, so a few timer turns are needed — not just microtasks.
async function settle(turns = 12) {
  for (let i = 0; i < turns; i += 1) {
    await new Promise((r) => setTimeout(r, 0));
  }
}

function routedFetch(routes = {}) {
  const calls = [];
  const fn = vi.fn(async (url, opts) => {
    calls.push({ url: String(url), opts });
    const u = String(url);
    if (u.endsWith("instance.json")) {
      if (routes.instanceThrows) throw new Error("instance.json is not JSON");
      return { ok: true, json: async () => ({ ...CONFIG }) };
    }
    if (u.includes("/api/quick_links/ops")) {
      return { ok: true, json: async () => ({ ok: true, quick_links: [] }) };
    }
    throw new Error("unrouted fetch " + u);
  });
  return { fn, calls };
}

async function loadServiceWorker({ alarms, seedLocal, fetchRoutes } = {}) {
  vi.resetModules();
  globalThis.chrome = createChromeMock({ alarms });
  if (seedLocal) await chrome.storage.local.set(seedLocal);
  globalThis.WebSocket = FakeWebSocket;
  const { fn, calls } = routedFetch(fetchRoutes);
  globalThis.fetch = fn;
  const mod = await import("../src/service-worker.js");
  await settle();
  return { mod, fetchCalls: calls, fetchFn: fn };
}

let errorSpy;
beforeEach(() => {
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  errorSpy.mockRestore();
});

// --- alarms: create ONLY what is missing (§6) --------------------------------
describe("alarm registration", () => {
  it("creates both periodic alarms on a cold start with none registered", async () => {
    await loadServiceWorker();
    expect(chrome.alarms._alarms[RECONNECT_ALARM]).toEqual({ periodInMinutes: 1 });
    expect(chrome.alarms._alarms[TICK_ALARM]).toEqual({ periodInMinutes: 1 });
  });

  it("does NOT re-create an alarm that already exists (phase is not reset)", async () => {
    // The failure this guards: a self-navigating tab (§5, the Grafana playlist) wakes
    // the worker every ~35–55 s and it dies in between. chrome.alarms.create on an
    // EXISTING alarm restarts its period, so every cold start pushes the next fire a
    // full 60 s out and the tick never happens — the watched tab ages unrecorded and
    // the opportunistic quick-links flush starves. Drop the alarms.get check and
    // `_created` is non-empty here.
    // The seeded objects carry a marker: `_created` being empty is a NEGATIVE assertion
    // that would also hold if ensureAlarms never ran, so the surviving marker is the
    // positive half — it proves the registration path executed and chose to leave the
    // existing alarms (and their phase) alone.
    await loadServiceWorker({
      alarms: {
        [RECONNECT_ALARM]: { periodInMinutes: 1, __seeded: "reconnect" },
        [TICK_ALARM]: { periodInMinutes: 1, __seeded: "tick" },
      },
    });
    expect(chrome.alarms._created).toEqual([]);
    expect(chrome.alarms._alarms[RECONNECT_ALARM].__seeded).toBe("reconnect");
    expect(chrome.alarms._alarms[TICK_ALARM].__seeded).toBe("tick");
  });

  it("creates only the MISSING one when a single alarm survived", async () => {
    await loadServiceWorker({ alarms: { [TICK_ALARM]: { periodInMinutes: 1 } } });
    expect(chrome.alarms._created).toEqual([RECONNECT_ALARM]);
  });
});

// --- §6 get_connection_state: REAL values -----------------------------------
describe("get_connection_state (§6)", () => {
  function ask() {
    const listener = chrome.runtime.onMessage.listeners[0];
    return new Promise((resolve) => {
      listener({ type: "get_connection_state" }, {}, resolve);
    });
  }

  it("reports connected + lastSeenAt after a hello_ack, and no reject reason", async () => {
    const { mod } = await loadServiceWorker();
    const before = Date.now();
    mod.connection.ws._open();
    mod.connection.ws._serverSend({ type: "hello_ack", ok: true });
    await settle(2);

    const state = await ask();
    expect(state.connected).toBe(true);
    expect(state.rejectReason).toBe(null);
    // A real timestamp, not the old hardcoded null.
    expect(state.lastSeenAt).toBeGreaterThanOrEqual(before);
  });

  it("reports the hello rejection code and connected:false", async () => {
    const { mod } = await loadServiceWorker();
    mod.connection.ws._open();
    mod.connection.ws._serverSend({
      type: "hello_ack",
      ok: false,
      error: { code: "duplicate_instance" },
    });
    await settle(2);

    const state = await ask();
    expect(state.connected).toBe(false);
    expect(state.rejectReason).toBe("duplicate_instance");
    expect(state.lastSeenAt).not.toBe(null);
    // Persisted in storage.session so a RESURRECTED worker still reports it (§6):
    // the facts must outlive this worker, or every cold start reads "never seen".
    const got = await chrome.storage.session.get(CONNECTION_STATE_KEY);
    expect(got[CONNECTION_STATE_KEY].rejectReason).toBe("duplicate_instance");
  });

  it("a worker that has not connected yet still reports the PERSISTED facts", async () => {
    const { mod } = await loadServiceWorker();
    mod.connection.ws._open();
    mod.connection.ws._serverSend({ type: "hello_ack", ok: true });
    await settle(2);
    const seenAt = (await ask()).lastSeenAt;
    // Positive control: without this the assertion below degenerates to null === null
    // and passes even when nothing was ever seen or persisted.
    expect(typeof seenAt).toBe("number");

    // A NEW worker over the SAME session storage: nothing observed in memory yet.
    const session = chrome.storage.session;
    vi.resetModules();
    const carriedOver = await session.get(CONNECTION_STATE_KEY);
    globalThis.chrome = createChromeMock();
    await chrome.storage.session.set({ [CONNECTION_STATE_KEY]: carriedOver[CONNECTION_STATE_KEY] });
    globalThis.WebSocket = FakeWebSocket;
    globalThis.fetch = routedFetch().fn;
    const fresh = await import("../src/service-worker.js");
    await settle();

    const state = await new Promise((resolve) =>
      chrome.runtime.onMessage.listeners[0]({ type: "get_connection_state" }, {}, resolve),
    );
    expect(fresh.connection.helloAcked).toBe(false); // honestly not acked yet
    expect(state.connected).toBe(false);
    expect(state.lastSeenAt).toBe(seenAt); // but the last sighting survived
  });

  it("persists a sighting from a NON-ack frame too (a ping after the ack)", async () => {
    // Persisting only inside the hello_ack branch throws away everything the worker saw
    // afterwards: it dies, comes back, and reports the ack-time lastSeenAt — the status
    // bar's "закрыт N назад" is then wrong by the whole interval since the ack (§10
    // tells "closed" from "stale" by exactly that number).
    const { mod } = await loadServiceWorker();
    mod.connection.ws._open();
    mod.connection.ws._serverSend({ type: "hello_ack", ok: true });
    await settle(2);
    const atAck = (await chrome.storage.session.get(CONNECTION_STATE_KEY))[CONNECTION_STATE_KEY]
      .lastSeenAt;

    await new Promise((r) => setTimeout(r, 5)); // time passes, then a heartbeat lands
    mod.connection.ws._serverSend({ type: "ping" });
    await settle(2);

    const persisted = (await chrome.storage.session.get(CONNECTION_STATE_KEY))[
      CONNECTION_STATE_KEY
    ];
    expect(persisted.lastSeenAt).toBeGreaterThan(atAck);
  });

  it("does NOT write storage on `command` frames (the hot path of a 200-tab pass)", async () => {
    // A storage write in front of every command dispatch buys nothing: the 15 s ping
    // keeps the persisted value fresh right through the pass, to well within the second
    // §10 needs. In-memory lastSeenAt still moves for every frame.
    const { mod } = await loadServiceWorker();
    mod.connection.ws._open();
    mod.connection.ws._serverSend({ type: "hello_ack", ok: true });
    await settle(2);

    const writes = vi.spyOn(chrome.storage.session, "set");
    for (let i = 0; i < 20; i += 1) {
      mod.connection.ws._serverSend({
        type: "command",
        id: "c" + i,
        sessionId: "nope", // stale_session: answered without touching the browser
        command: "get_tab",
        params: { tabId: 1 },
      });
    }
    await settle(4);
    expect(writes).not.toHaveBeenCalled();

    // ...but a heartbeat still records the sighting.
    mod.connection.ws._serverSend({ type: "ping" });
    await settle(2);
    expect(writes).toHaveBeenCalled();
  });

  it("a closed socket is no longer 'connected'", async () => {
    const { mod } = await loadServiceWorker();
    mod.connection.ws._open();
    mod.connection.ws._serverSend({ type: "hello_ack", ok: true });
    await settle(2);
    // Positive control FIRST: `connected === false` after the close is also the value
    // when the ack never landed, so the assertion only means something once we have
    // seen it be true.
    expect((await ask()).connected).toBe(true);

    mod.connection.ws.close();
    expect((await ask()).connected).toBe(false);
  });
});

// --- floating promises are logged, not unhandled (§6) ------------------------
describe("start-up failures are named", () => {
  it("a broken instance.json is LOGGED instead of becoming an unhandled rejection", async () => {
    await loadServiceWorker({ fetchRoutes: { instanceThrows: true } });
    const logged = errorSpy.mock.calls.map((c) => String(c[0]));
    expect(logged.some((m) => m.includes("ensureSocket failed"))).toBe(true);
  });

  it("the reconnect ALARM path is guarded too (every fire would otherwise reject)", async () => {
    await loadServiceWorker({ fetchRoutes: { instanceThrows: true } });
    errorSpy.mockClear();
    chrome.alarms.onAlarm._emit({ name: RECONNECT_ALARM });
    await settle(4);
    const logged = errorSpy.mock.calls.map((c) => String(c[0]));
    expect(logged.some((m) => m.includes("ensureSocket failed"))).toBe(true);
  });
});

// --- §10: a stranded claimed batch is revived on worker start ----------------
describe("quick-links queue revival on start (§10)", () => {
  it("re-sends a claimed batch left by a dead worker, under its ORIGINAL key", async () => {
    const { fetchCalls } = await loadServiceWorker({
      seedLocal: {
        [QUEUE_KEY]: {
          ops: [],
          claimed: { ops: [{ op: "add", url: "https://stranded" }], key: "key-from-dead-worker" },
        },
      },
    });

    const post = fetchCalls.find((c) => c.url.includes("/api/quick_links/ops"));
    expect(post).toBeDefined(); // nothing else would ever resend it
    expect(post.opts.headers["Idempotency-Key"]).toBe("key-from-dead-worker");
    expect(JSON.parse(post.opts.body)).toEqual([{ op: "add", url: "https://stranded" }]);
  });

  it("an empty queue posts nothing on start", async () => {
    const { fetchCalls } = await loadServiceWorker();
    expect(fetchCalls.some((c) => c.url.includes("/api/quick_links/ops"))).toBe(false);
  });
});
