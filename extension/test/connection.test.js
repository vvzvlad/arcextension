import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createChromeMock, FakeWebSocket, settle } from "./chrome-mock.js";
import { chromeEnv, Connection } from "../src/connection.js";
import { PROTOCOL_VERSION } from "../src/constants.js";

// instance.json is now only an OPTIONAL address bootstrap (§7): the shared token +
// self-reported instanceId are GONE. The credential is a per-profile secret.
const CONFIG = {
  title: "Test instance",
  serviceUrl: "wss://host.example",
};

// A known 32-byte secret (all 0x01). Option A: the RAW secret hex is what the wire carries
// (as `secret`) and what the /api Bearer is — the server hashes it, the client never does.
const SECRET_HEX = "01".repeat(32);
const APPROVED_FACTS = { requestPending: false, approved: true, quarantined: false, lastVerdict: null };

// hello/enroll read the secret from storage (several async ticks), so the opening frame
// lands a few macrotasks later than the old token-only path — and "a few" is not a number
// this file may guess at. `settle()` drains the mock until it reports nothing in flight,
// so the wait is as long as the machine needs and no longer; a fixed sleep asserted on a
// half-written chain whenever the box was busy. Nothing under extension/src schedules a
// timer of its own, so the mock's queue is the complete picture of the pending work.
const flush = () => settle();

// Seed an ENROLLED profile (secret + approved) so the socket opens and hello is sent.
async function seedEnrolled() {
  await chrome.storage.local.set({ instanceSecret: SECRET_HEX, enrollState: { ...APPROVED_FACTS } });
}

let savedFetch, savedWS;

beforeEach(async () => {
  globalThis.chrome = createChromeMock();
  savedFetch = globalThis.fetch;
  savedWS = globalThis.WebSocket;
  globalThis.fetch = async () => ({ json: async () => ({ ...CONFIG }) });
  globalThis.WebSocket = FakeWebSocket;
  await seedEnrolled();
});

afterEach(() => {
  globalThis.fetch = savedFetch;
  globalThis.WebSocket = savedWS;
});

// The real chromeEnv(), with its STORAGE seams bound to the mock of THIS test.
//
// chromeEnv()'s closures read `globalThis.chrome` at CALL time, while beforeEach installs
// a brand-new mock per test. Any storage write still in flight when a test ends therefore
// lands in the NEXT test's storage — and because the mock is whole-object last-write-wins,
// a late `enrollState` write silently replaces the state the next test just seeded. That
// produced phantom failures in whichever test ran next. Binding the seams here keeps a
// late write on the storage it belongs to; everything else still comes from the real env.
function testEnv() {
  const mock = globalThis.chrome;
  return {
    ...chromeEnv(),
    storageLocalGet: (key) => mock.storage.local.get(key),
    storageLocalSet: (obj) => mock.storage.local.set(obj),
    storageLocalRemove: (key) => mock.storage.local.remove(key),
    storageSessionGet: (key) => mock.storage.session.get(key),
    storageSessionSet: (obj) => mock.storage.session.set(obj),
  };
}

function makeConnection(overrides = {}) {
  const buildSnapshot =
    overrides.buildSnapshot ||
    (async (now, sessionId) => ({
      sessionId,
      focusedWindowId: 5,
      tabs: [{ tabId: 1, ageMs: 0, openedAgoMs: 0 }],
      windows: [{ id: 5, type: "normal", state: "normal" }],
    }));
  return new Connection(testEnv(), { buildSnapshot, commandHandler: overrides.commandHandler });
}

describe("ids & config (§6)", () => {
  it("generates installUuid in local, sessionId in session, reads instance.json", async () => {
    const conn = makeConnection();
    await conn.init();
    expect(conn.config).toMatchObject({ serviceUrl: "wss://host.example" });
    expect(conn.serviceAddress).toBe("wss://host.example");
    expect(conn.installUuid).toBeTruthy();
    expect(conn.sessionId).toBeTruthy();
    const local = await chrome.storage.local.get("installUuid");
    const session = await chrome.storage.session.get("sessionId");
    expect(local.installUuid).toBe(conn.installUuid);
    expect(session.sessionId).toBe(conn.sessionId);
  });

  it("reuses an existing installUuid across a re-init", async () => {
    await chrome.storage.local.set({ installUuid: "pre-existing" });
    const conn = makeConnection();
    await conn.init();
    expect(conn.installUuid).toBe("pre-existing");
  });
});

describe("hello (§6/§7)", () => {
  it("sends a secret-authenticated hello on socket open — NO token, NO self-reported id", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    // hello reads the secret + execute_js checkbox from storage.local first, so it is
    // sent on a macrotask — flush before asserting.
    await flush();
    expect(ws.sent).toHaveLength(1);
    expect(ws.sent[0]).toMatchObject({
      type: "hello",
      protocolVersion: PROTOCOL_VERSION,
      secret: SECRET_HEX,
      installUuid: conn.installUuid,
      sessionId: conn.sessionId,
      allowExecuteJs: false,
    });
    // The shared token and the self-reported instanceId are GONE from the wire (§7):
    // the server resolves the id from the secret. Their absence is the whole point.
    expect(ws.sent[0].token).toBeUndefined();
    expect(ws.sent[0].instanceId).toBeUndefined();
  });

  it("reports the checkbox state, NOT instance.json (gate default OFF, §12)", async () => {
    const conn = makeConnection();
    await conn.init();
    // instance.json opted in, but the per-copy checkbox is UNSET => the gate is
    // OFF, so hello must report false (else the service holds a false "can
    // execute_js" and sends a doomed execute_js). Old config-fallback => true.
    conn.config.allowExecuteJs = true;
    expect(await conn._readAllowExecuteJs()).toBe(false);
    // With the checkbox explicitly on, it reports true.
    await chrome.storage.local.set({ allowExecuteJs: true });
    expect(await conn._readAllowExecuteJs()).toBe(true);
  });
});

describe("helloAcked is ack-gated, not open-gated (§6)", () => {
  it("socket open does NOT mark acked; only hello_ack{ok:true} does", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    // Sending hello on open must NOT flip acked — otherwise a bad token would
    // read as connected and a reconnect loop would look healthy (§6).
    expect(conn.helloAcked).toBe(false);
    ws._serverSend({ type: "hello_ack", ok: true });
    await flush();
    expect(conn.helloAcked).toBe(true); // marked ONLY here
  });

  it("hello_ack{ok:false} keeps it unacked AND records why", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    conn.ws._serverSend({ type: "hello_ack", ok: false, error: { code: "auth" } });
    await flush();
    // `helloAcked === false` ALONE is vacuous: it is also the value when the message
    // never reached the handler at all (a broken onmessage wiring, a frame dropped by
    // JSON.parse). The reject reason is the POSITIVE half — it can only be set by the
    // handler having actually run on this frame — so the two together prove the
    // negative for the right reason (§6/§10: the four states must be distinguishable).
    expect(conn.helloAcked).toBe(false);
    expect(conn.rejectReason).toBe("auth");
    expect(conn.lastSeenAt).not.toBe(null);
  });
});

describe("snapshot_request / ping (§6)", () => {
  it("answers snapshot_request with a correlated snapshot frame", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    ws._serverSend({ type: "snapshot_request", id: "req-7" });
    await flush();
    const snap = ws.sent.find((m) => m.type === "snapshot");
    expect(snap).toBeDefined();
    expect(snap.id).toBe("req-7");
    expect(snap.sessionId).toBe(conn.sessionId);
    expect(snap.focusedWindowId).toBe(5);
    expect(snap.tabs).toHaveLength(1);
    expect(snap.windows).toHaveLength(1);
  });

  it("replies to ping with pong", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    ws._serverSend({ type: "ping" });
    await flush();
    expect(ws.sent.find((m) => m.type === "pong")).toBeDefined();
  });
});

describe("command execution wiring (§6)", () => {
  it("runs the handler with the current session and sends a correlated response", async () => {
    const commandHandler = vi.fn(async (_frame, ctx) => ({
      ok: true,
      result: { seenSession: ctx.sessionId },
    }));
    const conn = makeConnection({ commandHandler });
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    ws.sent.length = 0; // ignore the hello
    ws._serverSend({ type: "command", id: "c1", command: "get_tab", params: { tabId: 3 } });
    await flush();
    expect(commandHandler).toHaveBeenCalledOnce();
    // The handler is fed the extension's CURRENT session (for its own §5 check).
    expect(commandHandler.mock.calls[0][1].sessionId).toBe(conn.sessionId);
    const resp = ws.sent.find((m) => m.type === "response");
    expect(resp).toBeDefined();
    expect(resp.id).toBe("c1"); // correlated by id
    expect(resp.ok).toBe(true);
    expect(resp.result).toEqual({ seenSession: conn.sessionId });
  });

  it("still replies (internal error) when the handler throws", async () => {
    const commandHandler = vi.fn(async () => {
      throw new Error("boom");
    });
    const conn = makeConnection({ commandHandler });
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    ws.sent.length = 0;
    ws._serverSend({ type: "command", id: "c9", command: "get_tab", params: {} });
    await flush();
    const resp = ws.sent.find((m) => m.type === "response");
    expect(resp).toMatchObject({ id: "c9", ok: false, error: { code: "internal" } });
  });
});

describe("hello reports the STORED execute_js checkbox (§12)", () => {
  it("sends the storage.local value, overriding the instance.json default", async () => {
    // instance.json default is off (CONFIG.allowExecuteJs=false); the owner
    // ticked the options checkbox => storage.local wins and hello reports true.
    await chrome.storage.local.set({ allowExecuteJs: true });
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    await flush();
    expect(ws.sent[0]).toMatchObject({ type: "hello", allowExecuteJs: true });
  });
});

describe("ensureSocket idempotence (§6)", () => {
  it("does not open a second socket when one is already live", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const first = conn.ws;
    first._open();
    await conn.ensureSocket();
    expect(conn.ws).toBe(first);
  });
});

// ============================================================================
// Enrollment (§7, issue #35)
// ============================================================================

// A bare env (stubbable crypto) around the current chrome mock.
function bareConn(overrides = {}) {
  const env = { ...testEnv(), ...overrides };
  return new Connection(env, { buildSnapshot: async () => ({}) });
}

describe("secret (§7, option A: raw secret on the wire)", () => {
  it("generates a 32-byte secret ONCE, persists it as hex, and exposes it RAW as the /api Bearer", async () => {
    // Fresh profile: drop the beforeEach seed. Stub ONLY randomBytes (via the env seam).
    await chrome.storage.local.remove("instanceSecret");
    await chrome.storage.local.remove("enrollState");
    let rand = 0;
    const conn = bareConn({
      randomBytes: (n) => {
        rand += 1;
        return new Uint8Array(n).fill(1);
      },
    });
    await conn.submitEnrollment("WIN-CODE");
    const got = await chrome.storage.local.get("instanceSecret");
    expect(got.instanceSecret).toBe(SECRET_HEX); // stored as hex, from the env seam
    // Option A: the /api Bearer is the RAW secret hex — no client-side sha256 anymore.
    expect(await conn._apiSecret()).toBe(SECRET_HEX);
    // Generated ONCE: a second submit reuses the same secret.
    await conn.submitEnrollment("WIN-CODE-2");
    expect(rand).toBe(1);
  });
});

describe("enroll_request frame (§6/§7)", () => {
  it("sends enroll_request{code, secret, installUuid, instanceId} — NOT a hello", async () => {
    // Keep the seeded secret but make the state not-enrolled, and stage a name.
    await chrome.storage.local.set({
      enrollState: { requestPending: false, approved: false, quarantined: false, lastVerdict: null },
      browserName: "bobs-chrome",
    });
    const conn = makeConnection();
    await conn.submitEnrollment("WIN-CODE");
    const ws = conn.ws;
    ws._open();
    await flush();
    const req = ws.sent.find((m) => m.type === "enroll_request");
    expect(req).toBeDefined();
    expect(req).toMatchObject({
      type: "enroll_request",
      protocolVersion: PROTOCOL_VERSION,
      code: "WIN-CODE",
      secret: SECRET_HEX,
      installUuid: conn.installUuid,
      // ONE name for one thing: the field IS the instance id the service will assign
      // (§6). It used to be `title`, a display name paired with an id the operator typed
      // separately into the console — two names for a browser that is never renamed.
      instanceId: "bobs-chrome",
    });
    // Neither the old display-name spellings nor `origin` ride along. The frame used to
    // carry `title` AND `suggestedTitle` "to be safe", and an `origin` whose only reader
    // was the pending row an operator inspected before approving. All three are gone: a
    // field nobody reads is how a contract drifts.
    expect(req.title).toBeUndefined();
    expect(req.suggestedTitle).toBeUndefined();
    expect(req.origin).toBeUndefined();
    expect(ws.sent.find((m) => m.type === "hello")).toBeUndefined();
  });

  it("enroll_accepted enrols on the spot: id stored, approved, code cleared", async () => {
    // The headline of the one-step flow. There is no `enroll_pending` and no waiting: the
    // very frame that answers the request says the browser is IN, with the id it asked
    // for. Reddens if the client goes back to treating the answer as "recorded, awaiting
    // an operator" — the state would sit at needs-enroll over a live instance.
    await chrome.storage.local.set({
      enrollState: { requestPending: false, approved: false, quarantined: false, lastVerdict: null },
      browserName: "bobs-chrome",
    });
    const conn = makeConnection();
    await conn.submitEnrollment("WIN-CODE");
    conn.ws._open();
    await flush();
    expect(conn.ws.sent.find((m) => m.type === "enroll_request")).toBeDefined();

    conn.ws._serverSend({ type: "enroll_accepted", instanceId: "bobs-chrome" });
    await flush();

    expect(await conn.getEnrollState()).toBe("approved");
    expect((await chrome.storage.local.get("instanceId")).instanceId).toBe("bobs-chrome");
    // The one-shot window code has served its purpose and must not linger.
    expect((await chrome.storage.local.get("enrollCode")).enrollCode).toBeUndefined();
    const facts = (await chrome.storage.local.get("enrollState")).enrollState;
    expect(facts.approved).toBe(true);
    expect(facts.requestPending).toBe(false);
    // A COLD worker over the same storage reads the same verdict.
    expect(await makeConnection().getEnrollState()).toBe("approved");
  });

  it("there is no intermediate 'pending' state between not-enrolled and active", async () => {
    // A submitted-but-unanswered attempt reads needs-enroll, which is the truth: the
    // service holds nothing for it. The old `pending` meant "an operator has yet to look
    // at your request", and there is nobody to look. Reddens if the state is reintroduced
    // — every surface would again show "ожидает одобрения" over a request that either
    // never arrived or was refused.
    await chrome.storage.local.set({
      enrollState: { requestPending: false, approved: false, quarantined: false, lastVerdict: null },
      browserName: "bobs-chrome",
    });
    const conn = makeConnection();
    await conn.submitEnrollment("WIN-CODE");
    conn.ws._open();
    await flush();
    expect(await conn.getEnrollState()).toBe("needs-enroll");
    expect((await conn.getConnectionState()).enrollState).toBe("needs-enroll");
  });

  it("re-sends the request on every reconnect until it is accepted (submit with no open socket)", async () => {
    // The operator pressed submit while the service was down: the forced reconnect never
    // opened, so the enroll_request never went out. The next opening frame must be the
    // enroll_request, not a hello that can only ever answer `unknown_instance`.
    //
    // The old client re-sent only while `requestRegisteredAt === null`, because a
    // confirmed request was waiting in an operator's list and re-registering it was
    // pointless churn. Nothing waits anywhere now, so the condition collapses to "a code
    // is staged and we are not enrolled", and the resend costs exactly one frame that is
    // answered immediately.
    await chrome.storage.local.set({
      enrollCode: "WIN-CODE",
      browserName: "bobs-chrome",
      enrollState: {
        requestPending: true,
        approved: false,
        quarantined: false,
        lastVerdict: null,
      },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    const req = conn.ws.sent.find((m) => m.type === "enroll_request");
    expect(req).toBeDefined();
    expect(req.code).toBe("WIN-CODE");
    expect(conn.ws.sent.find((m) => m.type === "hello")).toBeUndefined();
  });

  it("stops re-sending once the code is gone, and hellos instead", async () => {
    // A terminal refusal clears the staged code (below); after that the opening frame
    // falls back to hello, so a browser whose enrolment was refused does not hammer the
    // service with a frame that can only be refused again.
    await chrome.storage.local.set({
      browserName: "bobs-chrome",
      enrollState: {
        requestPending: true,
        approved: false,
        quarantined: false,
        lastVerdict: null,
        enrollReject: "id_taken",
      },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    expect(conn.ws.sent.find((m) => m.type === "enroll_request")).toBeUndefined();
    expect(conn.ws.sent.find((m) => m.type === "hello")).toBeDefined();
  });

  it("a NAME refusal is terminal: the reason is stored and the code is dropped", async () => {
    // id_taken / bad_id are about the NAME, not the code: the window may well still be
    // open, but re-sending the same frame draws the same answer forever. The operator has
    // to change the name and submit again. And the reason must be DURABLE, because the
    // extension settings are the ONLY place a human ever sees it — /admin has no list of
    // refused attempts anymore.
    for (const reason of ["id_taken", "bad_id"]) {
      await chrome.storage.local.set({
        enrollCode: "WIN-CODE",
        browserName: "bobs-chrome",
        enrollState: {
          requestPending: true,
          approved: false,
          quarantined: false,
          lastVerdict: null,
        },
      });
      const conn = makeConnection();
      await conn.ensureSocket();
      conn.ws._open();
      await flush();
      conn.ws._serverSend({ type: "enroll_rejected", reason });
      await flush();

      const facts = (await chrome.storage.local.get("enrollState")).enrollState;
      expect(facts.enrollReject).toBe(reason);
      expect((await chrome.storage.local.get("enrollCode")).enrollCode).toBeUndefined();
      // A COLD worker reports it too — the settings page is opened by one.
      expect((await makeConnection().getConnectionState()).enrollReject).toBe(reason);
    }
  });

  it("a TRANSIENT refusal keeps the code so the next opening retries", async () => {
    // A protocol skew during a rollout resolves itself; the staged code is still good.
    await chrome.storage.local.set({
      enrollCode: "WIN-CODE",
      browserName: "bobs-chrome",
      enrollState: {
        requestPending: true,
        approved: false,
        quarantined: false,
        lastVerdict: null,
      },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    conn.ws._serverSend({ type: "enroll_rejected", reason: "protocol" });
    await flush();
    expect((await chrome.storage.local.get("enrollCode")).enrollCode).toBe("WIN-CODE");

    const cold = makeConnection();
    await cold.ensureSocket();
    cold.ws._open();
    await flush();
    expect(cold.ws.sent.find((m) => m.type === "enroll_request")).toBeDefined();
  });
});

describe("hello_ack{ok:false} verdicts (§7)", () => {
  it("`revoked` WIPES the secret and drops to needs-enroll (records lastVerdict)", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    await flush();
    expect((await chrome.storage.local.get("instanceSecret")).instanceSecret).toBe(SECRET_HEX);
    ws._serverSend({ type: "hello_ack", ok: false, error: { code: "revoked" } });
    await flush();
    expect((await chrome.storage.local.get("instanceSecret")).instanceSecret).toBeUndefined();
    expect(await conn.getEnrollState()).toBe("revoked"); // secret gone + lastVerdict='revoked'
  });

  it("a REVOKED instance opens NO idle socket on the next alarm (nothing to send)", async () => {
    // Durable revoked state: the secret is already wiped, so a hello would carry no
    // secret. Connecting anyway would hold/reopen an idle pre-auth socket every alarm
    // across the whole revoked fleet. The gate must treat revoked like needs-enroll.
    await chrome.storage.local.remove("instanceSecret");
    await chrome.storage.local.remove("instanceSecretPending");
    await chrome.storage.local.set({
      serviceAddress: "wss://host.example",
      enrollState: { requestPending: false, approved: false, quarantined: false, lastVerdict: "revoked" },
    });
    const conn = makeConnection();
    expect(await conn.getEnrollState()).toBe("revoked");
    await conn.ensureSocket();
    expect(conn.ws).toBe(null); // no idle socket
  });

  it("`unknown_instance` KEEPS the secret and stages a fresh pending re-enroll (staged code)", async () => {
    await chrome.storage.local.set({ enrollCode: "NEW-CODE" });
    const oldSecret = (await chrome.storage.local.get("instanceSecret")).instanceSecret;
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    await flush();

    // A previously-approved instance suddenly unknown => quarantine WITHOUT wiping.
    ws._serverSend({ type: "hello_ack", ok: false, error: { code: "unknown_instance" } });
    await flush();
    const active = (await chrome.storage.local.get("instanceSecret")).instanceSecret;
    const pending = (await chrome.storage.local.get("instanceSecretPending")).instanceSecretPending;
    expect(active).toBe(oldSecret); // OLD secret KEPT (no fleet-timer walk)
    expect(pending).toBeTruthy(); // a fresh secret staged for the parallel re-enroll
    expect(pending).not.toBe(oldSecret);
    expect(await conn.getEnrollState()).toBe("quarantined");
  });

  it("survives worker death: a COLD worker sends enroll_request for the PENDING secret (invariant C)", async () => {
    // Quarantine first (staged code), then throw away the live worker and prove a COLD
    // Connection over the same storage reconstitutes the code from ENROLL_CODE_KEY and
    // sends an enroll_request for the PENDING secret — NOT a doomed hello. Reverting the
    // cold-worker code reconstitution reddens this (it would send a hello instead).
    await chrome.storage.local.set({ enrollCode: "NEW-CODE" });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    conn.ws._serverSend({ type: "hello_ack", ok: false, error: { code: "unknown_instance" } });
    await flush();
    const pendingHex = (await chrome.storage.local.get("instanceSecretPending")).instanceSecretPending;

    // COLD worker (probe cursor is DURABLE at 0): open a fresh socket.
    const cold = makeConnection();
    await cold.ensureSocket();
    cold.ws._open();
    await flush();
    const req = cold.ws.sent.find((m) => m.type === "enroll_request");
    expect(req).toBeDefined();
    expect(req.code).toBe("NEW-CODE");
    expect(req.secret).toBe(pendingHex); // enrolls the PENDING secret (raw on the wire)
    expect(cold.ws.sent.find((m) => m.type === "hello")).toBeUndefined();
  });

  it("keeps attempting HELLO with the OLD secret while quarantined (invariant A — no shadowing)", async () => {
    // Seed a quarantine mid-cycle at the OLD-hello probe phase (1). A cold worker must
    // hello with the OLD secret so a transient `unknown` that healed server-side recovers
    // — never permanently shadowed by the unenrolled pending secret. Reverting the
    // old-vs-pending fix (always helloing pending) reddens this.
    const pendingHex = "ab".repeat(32);
    await chrome.storage.local.set({
      instanceSecret: SECRET_HEX, // OLD (approved) secret
      instanceSecretPending: pendingHex, // staged re-enroll secret
      enrollCode: "NEW-CODE",
      enrollState: {
        requestPending: true,
        approved: false,
        quarantined: true,
        lastVerdict: "unknown_instance",
        quarantineProbe: 1, // the hello(old) phase
      },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    const hello = conn.ws.sent.find((m) => m.type === "hello");
    expect(hello).toBeDefined();
    expect(hello.secret).toBe(SECRET_HEX); // the OLD secret (raw), not the pending one
  });

  it("OLD-secret hello succeeding while quarantined = transient recovery: discard pending, keep old (invariant A)", async () => {
    const pendingHex = "ab".repeat(32);
    await chrome.storage.local.set({
      instanceSecret: SECRET_HEX,
      instanceSecretPending: pendingHex,
      enrollCode: "NEW-CODE",
      enrollState: {
        requestPending: true,
        approved: false,
        quarantined: true,
        lastVerdict: "unknown_instance",
        quarantineProbe: 1, // hello(old)
      },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    // The server RE-KNOWS the old secret (restore completed) => ok:true on the old hello.
    conn.ws._serverSend({ type: "hello_ack", ok: true, instanceId: "srv-old" });
    await flush();
    expect((await chrome.storage.local.get("instanceSecret")).instanceSecret).toBe(SECRET_HEX); // OLD kept
    expect((await chrome.storage.local.get("instanceSecretPending")).instanceSecretPending).toBeUndefined(); // moot re-enroll dropped
    expect(await conn.getEnrollState()).toBe("approved");
  });

  it("PENDING-secret hello succeeding = re-enroll approved: promote pending, wipe old (invariant B)", async () => {
    const pendingHex = "ab".repeat(32);
    await chrome.storage.local.set({
      instanceSecret: SECRET_HEX,
      instanceSecretPending: pendingHex,
      enrollCode: "NEW-CODE",
      enrollState: {
        requestPending: true,
        approved: false,
        quarantined: true,
        lastVerdict: "unknown_instance",
        quarantineProbe: 2, // the hello(pending) phase
      },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    const hello = conn.ws.sent.find((m) => m.type === "hello");
    expect(hello.secret).toBe(pendingHex); // helloed with the PENDING secret (raw on the wire)
    // The operator approved the re-enrollment => ok:true on the pending hello.
    conn.ws._serverSend({ type: "hello_ack", ok: true, instanceId: "srv-new" });
    await flush();
    expect((await chrome.storage.local.get("instanceSecret")).instanceSecret).toBe(pendingHex); // promoted (old wiped)
    expect((await chrome.storage.local.get("instanceSecretPending")).instanceSecretPending).toBeUndefined();
    expect((await chrome.storage.local.get("instanceId")).instanceId).toBe("srv-new");
    expect((await chrome.storage.local.get("enrollCode")).enrollCode).toBeUndefined(); // staged code cleared
    expect(await conn.getEnrollState()).toBe("approved");
  });

  it("surfaces an enroll_rejected reason through get_connection_state (durable, cold-worker readable)", async () => {
    await chrome.storage.local.set({
      enrollState: { requestPending: true, approved: false, quarantined: false, lastVerdict: null },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    conn.ws._serverSend({ type: "enroll_rejected", reason: "bad_code" });
    await flush();
    // A brand-new COLD worker over the same storage must still report the reason.
    const cold = makeConnection();
    const st = await cold.getConnectionState();
    expect(st.enrollReject).toBe("bad_code");
  });

  it("a terminal enroll_rejected(bad_code) clears the staged code so the probe stops re-sending enroll_request", async () => {
    const pendingHex = "ab".repeat(32);
    const quarantined = {
      requestPending: true,
      approved: false,
      quarantined: true,
      lastVerdict: "unknown_instance",
      quarantineProbe: 0, // the enroll_request(pending) phase
    };
    await chrome.storage.local.set({
      instanceSecret: SECRET_HEX,
      instanceSecretPending: pendingHex,
      enrollCode: "STALE-CODE",
      enrollState: { ...quarantined },
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    await flush();
    // Phase 0 sent the enroll_request with the (now stale) code.
    const req = conn.ws.sent.find((m) => m.type === "enroll_request");
    expect(req).toBeDefined();
    expect(req.code).toBe("STALE-CODE");

    // The server rejects it terminally: the code is dead.
    conn.ws._serverSend({ type: "enroll_rejected", reason: "bad_code" });
    await flush();
    expect((await chrome.storage.local.get("enrollCode")).enrollCode).toBeUndefined(); // cleared

    // A fresh COLD worker back at phase 0 must NOT re-send enroll_request (no code) — it
    // falls back to a hello instead. Reverting the terminal-clear reddens this (a stale
    // code would re-arm enroll_request forever).
    await chrome.storage.local.set({ enrollState: { ...quarantined } }); // probe back to 0
    const cold = makeConnection();
    await cold.ensureSocket();
    cold.ws._open();
    await flush();
    expect(cold.ws.sent.find((m) => m.type === "enroll_request")).toBeUndefined(); // no more spam
    expect(cold.ws.sent.find((m) => m.type === "hello")).toBeDefined(); // falls back to hello
  });

  it("a never-enrolled `unknown_instance` just keeps trying — no quarantine, no re-enroll", async () => {
    // A secret exists but was never accepted, so `unknown` is the ONLY answer a hello can
    // draw. Quarantine is for a PREVIOUSLY approved instance that suddenly goes unknown
    // (a restore from an old backup); treating this one as quarantined would stage a
    // second secret for nothing.
    await chrome.storage.local.set({
      enrollState: { requestPending: true, approved: false, quarantined: false, lastVerdict: null },
      enrollCode: "SOME-CODE",
    });
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    await flush();
    ws._serverSend({ type: "hello_ack", ok: false, error: { code: "unknown_instance" } });
    await flush();
    // Still not enrolled; NOT quarantined, and no pending secret was staged.
    expect(await conn.getEnrollState()).toBe("needs-enroll");
    expect((await chrome.storage.local.get("instanceSecretPending")).instanceSecretPending).toBeUndefined();
  });
});

describe("socket identity guard (§6/§7)", () => {
  it("ignores a late frame from a PREEMPTED socket (structural no-wrong-promotion)", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const oldWs = conn.ws;
    oldWs._open();
    await flush();
    expect(conn.helloAcked).toBe(false);
    // Preempt the socket, as a quarantine forced-reconnect does: this.ws is swapped.
    conn.connect();
    expect(conn.ws).not.toBe(oldWs);
    // A late hello_ack lands on the OLD socket — the identity guard must drop it so it
    // cannot flip helloAcked or drive a promotion against the CURRENT connection's state.
    oldWs._serverSend({ type: "hello_ack", ok: true, instanceId: "srv-stale" });
    await flush();
    expect(conn.helloAcked).toBe(false); // not marked connected by the preempted socket
    expect((await chrome.storage.local.get("instanceId")).instanceId).toBeUndefined();
  });
});

// ============================================================================
// The service address is a TLS gate, not a hint (§7)
// ============================================================================
describe("service address gate (§7)", () => {
  // Option A puts the RAW secret on the wire — in the enroll_request, in every hello and
  // as the /api Bearer — so an unencrypted address does not degrade the system, it
  // publishes a permanent credential. The client must refuse to open the socket at all.
  it("does NOT connect over a plaintext ws:// address, and says why", async () => {
    await chrome.storage.local.set({ serviceAddress: "ws://curator.lan:8000" });
    const conn = makeConnection();
    await conn.ensureSocket();
    expect(conn.ws).toBe(null); // no socket => the secret never leaves this profile

    const st = await conn.getConnectionState();
    expect(st.hasAddress).toBe(false);
    // Distinguishable from "never configured": the operator typed something and must be
    // told it was refused, not that the field is empty.
    expect(st.addressError).toBe("insecure");
  });

  it("DIALS an https:// site URL as wss:// — same TLS, same host", async () => {
    // Pasting the service URL out of the address bar is how this field actually gets
    // filled. https:// is not a security problem (it is the very TLS the gate demands),
    // so it is upgraded rather than refused.
    await chrome.storage.local.set({ serviceAddress: "https://curator.example" });
    const conn = makeConnection();
    await conn.ensureSocket();
    expect(conn.ws).not.toBe(null);
    expect(conn.ws.url).toBe("wss://curator.example/ext");
    expect((await conn.getConnectionState()).addressError).toBe(null);
  });

  it("refuses a plaintext http:// site URL where the socket URL belongs", async () => {
    await chrome.storage.local.set({ serviceAddress: "http://curator.example" });
    const conn = makeConnection();
    await conn.ensureSocket();
    expect(conn.ws).toBe(null);
    expect((await conn.getConnectionState()).addressError).toBe("http-scheme");
  });

  it("refuses a bundle instance.json bootstrap that ships a ws:// serviceUrl", async () => {
    // The bootstrap address goes through the SAME gate: a bundle cannot smuggle plaintext
    // past a setting the operator never touched.
    globalThis.fetch = async () => ({ json: async () => ({ serviceUrl: "ws://curator.lan" }) });
    const conn = makeConnection();
    await conn.ensureSocket();
    expect(conn.ws).toBe(null);
    expect((await conn.getConnectionState()).addressError).toBe("insecure");
  });

  it("allows ws:// on loopback (development) and wss:// anywhere", async () => {
    await chrome.storage.local.set({ serviceAddress: "ws://localhost:8000" });
    const dev = makeConnection();
    await dev.ensureSocket();
    expect(dev.ws).not.toBe(null);
    expect((await dev.getConnectionState()).addressError).toBe(null);

    await chrome.storage.local.set({ serviceAddress: "wss://curator.example/" });
    const prod = makeConnection();
    await prod.ensureSocket();
    expect(prod.ws).not.toBe(null);
    expect(prod.ws.url).toBe("wss://curator.example/ext");
  });

  it("refuses to submit an enrollment against a refused address (no phantom 'pending')", async () => {
    // Arming the durable pending facts here would make every surface report "ожидает
    // одобрения" for a request that cannot leave the machine.
    await chrome.storage.local.remove("instanceSecret"); // fresh profile
    await chrome.storage.local.remove("enrollState");
    await chrome.storage.local.set({ serviceAddress: "ws://curator.lan:8000" });
    const conn = makeConnection();
    await expect(conn.submitEnrollment("WIN-CODE")).rejects.toThrow(/refused/i);
    expect((await chrome.storage.local.get("enrollState")).enrollState).toBeUndefined();
    expect((await chrome.storage.local.get("instanceSecret")).instanceSecret).toBeUndefined();
    expect(await conn.getEnrollState()).toBe("needs-enroll"); // NOT "pending"
  });

  it("hides the /api credential too while the address is refused", async () => {
    // The startpage and the popup take the Bearer from the SW; a refused address must not
    // resolve for them either, or the raw secret still goes out over plain http.
    await chrome.storage.local.set({ serviceAddress: "ws://curator.lan:8000" });
    const conn = makeConnection();
    await conn.init();
    expect(await conn._resolveAddress()).toBe(null);
  });
});

describe("enrollState from durable storage with a COLD worker (§7)", () => {
  it("an enrolled instance reads as approved even though helloAcked is false", async () => {
    // The operator's address is a durable setting; the worker is cold (never connected).
    await chrome.storage.local.set({ serviceAddress: "wss://host.example" });
    const conn = makeConnection();
    expect(conn.helloAcked).toBe(false); // cold — in-memory says "not connected"
    const st = await conn.getConnectionState();
    expect(st.connected).toBe(false); // honestly not connected right now
    expect(st.enrollState).toBe("approved"); // but DURABLY enrolled (from storage facts)
    expect(st.hasAddress).toBe(true);
  });

  it("a fresh profile (no secret) reads as needs-enroll and does NOT open a socket", async () => {
    await chrome.storage.local.remove("instanceSecret");
    await chrome.storage.local.remove("enrollState");
    const conn = makeConnection();
    expect(await conn.getEnrollState()).toBe("needs-enroll");
    await conn.ensureSocket();
    expect(conn.ws).toBe(null); // nothing to say => no churn before enrollment
  });
});
