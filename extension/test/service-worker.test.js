import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createChromeMock, FakeWebSocket } from "./chrome-mock.js";
import { RECONNECT_ALARM, TICK_ALARM, CONNECTION_STATE_KEY } from "../src/constants.js";
import { QUEUE_KEY } from "../src/quicklinks.js";

// The service worker is an ENTRY module: importing it registers the chrome listeners
// and runs its start-up work (alarms, socket, stranded-flush). So every test imports
// it FRESH (vi.resetModules) against a fresh chrome mock + globals.

const CONFIG = { title: "Prox", serviceUrl: "wss://host.example/" };

// An enrolled instance: a 32-byte secret in storage.local + the approved enroll fact.
// The SW only opens a socket once there is something to say (§7), so socket-level tests
// seed this so the connection actually connects. serviceUrl comes from CONFIG (the
// instance.json bootstrap fallback resolves the address).
const SECRET_HEX = "01".repeat(32);
const ENROLLED_SEED = {
  instanceSecret: SECRET_HEX,
  enrollState: { requestPending: false, approved: true, quarantined: false, lastVerdict: null },
};

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

async function loadServiceWorker({ alarms, seedLocal, fetchRoutes, notEnrolled, WebSocketImpl } = {}) {
  vi.resetModules();
  globalThis.chrome = createChromeMock({ alarms });
  // Enroll by default so the socket opens; a test wanting the not-enrolled gate passes
  // notEnrolled:true. Explicit seedLocal is merged on top.
  if (!notEnrolled) await chrome.storage.local.set({ ...ENROLLED_SEED });
  if (seedLocal) await chrome.storage.local.set(seedLocal);
  globalThis.WebSocket = WebSocketImpl || FakeWebSocket;
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
    // A successful ack now also promotes/records durable enroll facts (async
    // storage.local writes) before its trailing session persist — let ALL of that
    // settle so the spy below only sees writes the command frames would cause.
    await settle(8);

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

// --- §7 enrollment message channel (get_credential / submit / enrollState) ---
describe("enrollment message channel (§7)", () => {
  const SECRET_HASH = "72cd6e8422c407fb6d098690f1130b7ded7ec2f7f5e1d30bd9d521f015363793";
  function ask(message) {
    const listener = chrome.runtime.onMessage.listeners[0];
    return new Promise((resolve) => listener(message, {}, resolve));
  }

  it("get_credential returns the address + the instance secretHash (slice C Bearer)", async () => {
    await loadServiceWorker();
    const cred = await ask({ type: "get_credential" });
    // Address falls back to the instance.json bootstrap serviceUrl; the Bearer is the
    // sha256 of the seeded secret — the raw secret never leaves the SW.
    expect(cred.serviceUrl).toBe("wss://host.example/");
    expect(cred.secretHash).toBe(SECRET_HASH);
  });

  it("get_connection_state carries the durable enrollState (approved when seeded)", async () => {
    await loadServiceWorker();
    const st = await ask({ type: "get_connection_state" });
    expect(st.enrollState).toBe("approved");
    expect(st.hasAddress).toBe(true);
  });

  it("get_identity returns the server-assigned id from storage, not instance.json", async () => {
    await loadServiceWorker({ seedLocal: { instanceId: "srv-9", browserName: "Lab" } });
    const id = await ask({ type: "get_identity" });
    expect(id.instanceId).toBe("srv-9");
    expect(id.title).toBe("Lab");
  });

  it("submit_enrollment marks the request pending and acks ok", async () => {
    // A fresh profile: no secret yet. Submitting generates it + flags pending.
    await loadServiceWorker({ notEnrolled: true, seedLocal: { serviceAddress: "wss://host.example" } });
    const res = await ask({ type: "submit_enrollment", code: "WIN-CODE" });
    expect(res.ok).toBe(true);
    await settle(6);
    const facts = (await chrome.storage.local.get("enrollState")).enrollState;
    expect(facts.requestPending).toBe(true);
    expect((await chrome.storage.local.get("instanceSecret")).instanceSecret).toBeTruthy();
  });
});

// --- instance.json is OPTIONAL now (§7); floating promises stay guarded ------
describe("start-up robustness", () => {
  it("a broken/absent instance.json does NOT crash the worker (it is optional now, §7)", async () => {
    // Enrolled via storage.local, so the address falls back to nothing when
    // instance.json throws — the SW must tolerate it (no ensureSocket-failed spam) and
    // still register its message channel.
    await loadServiceWorker({ fetchRoutes: { instanceThrows: true } });
    const logged = errorSpy.mock.calls.map((c) => String(c[0]));
    expect(logged.some((m) => m.includes("ensureSocket failed"))).toBe(false);
    // Positive control: the worker booted far enough to wire its onMessage channel.
    expect(chrome.runtime.onMessage.listeners.length).toBeGreaterThan(0);
  });

  it("the reconnect ALARM path is guarded (a throwing socket ctor would otherwise reject)", async () => {
    // A WebSocket that throws on construction makes connect() (hence ensureSocket)
    // reject; the alarm handler's .catch must name it rather than leak an unhandled
    // rejection once per fire. Seed a serviceAddress so the enrolled instance actually
    // tries to connect.
    class ThrowingWS {
      constructor() {
        throw new Error("socket ctor boom");
      }
    }
    await loadServiceWorker({
      seedLocal: { serviceAddress: "wss://host.example" },
      WebSocketImpl: ThrowingWS,
    });
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
