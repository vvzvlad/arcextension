import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createChromeMock, FakeWebSocket } from "./chrome-mock.js";
import { chromeEnv, Connection } from "../src/connection.js";
import { PROTOCOL_VERSION } from "../src/constants.js";

const CONFIG = {
  instanceId: "inst-1",
  title: "Test instance",
  serviceUrl: "wss://host.example",
  token: "the-token",
  allowExecuteJs: false,
};

const flush = () => new Promise((r) => setTimeout(r, 5));

let savedFetch, savedWS;

beforeEach(() => {
  globalThis.chrome = createChromeMock();
  savedFetch = globalThis.fetch;
  savedWS = globalThis.WebSocket;
  globalThis.fetch = async () => ({ json: async () => ({ ...CONFIG }) });
  globalThis.WebSocket = FakeWebSocket;
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
  return new Connection(chromeEnv(), { buildSnapshot, onCommand: overrides.onCommand });
}

describe("ids & config (§6)", () => {
  it("generates installUuid in local, sessionId in session, reads instance.json", async () => {
    const conn = makeConnection();
    await conn.init();
    expect(conn.config).toMatchObject({ instanceId: "inst-1", serviceUrl: "wss://host.example" });
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

describe("hello (§6)", () => {
  it("sends a well-formed hello on socket open", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    expect(ws.sent).toHaveLength(1);
    expect(ws.sent[0]).toMatchObject({
      type: "hello",
      protocolVersion: PROTOCOL_VERSION,
      token: "the-token",
      instanceId: "inst-1",
      installUuid: conn.installUuid,
      sessionId: conn.sessionId,
      allowExecuteJs: false,
    });
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

  it("hello_ack{ok:false} keeps it unacked", async () => {
    const conn = makeConnection();
    await conn.ensureSocket();
    conn.ws._open();
    conn.ws._serverSend({ type: "hello_ack", ok: false, error: { code: "auth" } });
    await flush();
    expect(conn.helloAcked).toBe(false);
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

describe("command hook (NEXT phase) (§6)", () => {
  it("hands a command frame to the hook and sends NO response (verbs deferred)", async () => {
    const onCommand = vi.fn();
    const conn = makeConnection({ onCommand });
    await conn.ensureSocket();
    const ws = conn.ws;
    ws._open();
    ws.sent.length = 0; // ignore the hello
    ws._serverSend({ type: "command", id: "c1", command: "open_tab", params: {} });
    await flush();
    expect(onCommand).toHaveBeenCalledOnce();
    expect(ws.sent.find((m) => m.type === "response")).toBeUndefined();
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
