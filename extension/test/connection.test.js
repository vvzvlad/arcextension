import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createChromeMock, FakeWebSocket } from "./chrome-mock.js";
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
// lands a few macrotasks later than the old token-only path.
const flush = () => new Promise((r) => setTimeout(r, 25));

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

function makeConnection(overrides = {}) {
  const buildSnapshot =
    overrides.buildSnapshot ||
    (async (now, sessionId) => ({
      sessionId,
      focusedWindowId: 5,
      tabs: [{ tabId: 1, ageMs: 0, openedAgoMs: 0 }],
      windows: [{ id: 5, type: "normal", state: "normal" }],
    }));
  return new Connection(chromeEnv(), { buildSnapshot, commandHandler: overrides.commandHandler });
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
  const env = { ...chromeEnv(), ...overrides };
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

describe("enroll_request frame (§2/§7)", () => {
  it("sends enroll_request{code, secret, installUuid, suggestedTitle} — NOT a hello", async () => {
    // Keep the seeded secret but make the state not-approved, and stage a browser name.
    await chrome.storage.local.set({
      enrollState: { requestPending: false, approved: false, quarantined: false, lastVerdict: null },
      browserName: "Bob's Chrome",
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
      suggestedTitle: "Bob's Chrome", // documented frame field
      title: "Bob's Chrome", // what the server actually reads (wire contract)
    });
    expect(ws.sent.find((m) => m.type === "hello")).toBeUndefined();
  });

  it("after a pending submit a FRESH worker sends HELLO (not enroll_request) to learn approval (acc 5)", async () => {
    // Durable: secret + requestPending, but a fresh worker has NO in-memory code.
    await chrome.storage.local.set({
      enrollState: { requestPending: true, approved: false, quarantined: false, lastVerdict: null },
    });
    const conn = makeConnection(); // _pendingEnrollCode is null (cold)
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    await flush();
    expect(ws.sent.find((m) => m.type === "hello")).toBeDefined();
    expect(ws.sent.find((m) => m.type === "enroll_request")).toBeUndefined();
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

  it("a not-yet-approved `unknown_instance` just keeps waiting — no quarantine, no re-enroll", async () => {
    // Pending (submitted, not approved). unknown is the NORMAL not-approved-yet answer.
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
    // Still pending; NOT quarantined, and no pending secret was staged.
    expect(await conn.getEnrollState()).toBe("pending");
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
