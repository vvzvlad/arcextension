// Connection lifecycle (§6) — the REPORTING half: read instance config, own the
// ids, connect the WSS /ext socket, say hello, answer snapshot_request and ping.
// Command RECEIVING / execution (open_tab/close_tab/execute_js) is the NEXT
// phase — a `command` frame is logged and handed to an injectable hook here, the
// verbs are NOT implemented.
//
// Reconnect is driven by chrome.alarms (period TICK_MS = 60 s), NOT a timer
// inside the worker: the alarm handler unconditionally ensures an open socket,
// independent of in-memory state, and at 60 s the worker actually dies and gets
// resurrected so the mechanism is exercised (§6). Reconnect is alarm-only at a
// FIXED 60 s period, which is itself what stops a bad token from hammering once a
// second (the donor's bug, §6). Connection state is ack-gated: `helloAcked` flips
// true ONLY on hello_ack{ok:true}, never on socket `open`. A real backoff counter
// belongs with the deferred sub-minute "no ping" retry timer; there is no
// sub-alarm retry yet, so none is kept here.
//
// The module is dependency-injected via an `env` so the same code runs under the
// browser (real chrome/WebSocket globals) and under vitest (a fake env).

import { PROTOCOL_VERSION } from "./constants.js";

// Build the default environment from browser globals. Kept tiny; every capability
// is overridable in tests.
export function chromeEnv() {
  return {
    // Read instance.json fresh on every SW start — authoritative over storage
    // (§6 "Откуда берётся конфигурация инстанса").
    getInstanceConfig: async () => {
      const url = chrome.runtime.getURL("instance.json");
      const resp = await fetch(url);
      return await resp.json();
    },
    storageLocalGet: (key) => chrome.storage.local.get(key),
    storageLocalSet: (obj) => chrome.storage.local.set(obj),
    storageSessionGet: (key) => chrome.storage.session.get(key),
    storageSessionSet: (obj) => chrome.storage.session.set(obj),
    randomUUID: () => crypto.randomUUID(),
    WebSocketImpl: WebSocket,
    now: () => Date.now(),
    log: (...args) => console.log("[ext]", ...args),
  };
}

import {
  INSTALL_UUID_KEY,
  SESSION_ID_KEY,
} from "./constants.js";

// installUuid lives in chrome.storage.local (in the profile, NOT copied with the
// bundle — that is the whole point, §6): a copied instance.json shares instanceId
// but not installUuid, so a duplicate is detectable. Generate once.
async function ensureInstallUuid(env) {
  const got = await env.storageLocalGet(INSTALL_UUID_KEY);
  let uuid = got && got[INSTALL_UUID_KEY];
  if (!uuid) {
    uuid = env.randomUUID();
    await env.storageLocalSet({ [INSTALL_UUID_KEY]: uuid });
  }
  return uuid;
}

// sessionId lives in chrome.storage.session — it dies with the browser and is the
// session epoch (§5). Generate once per browser session.
async function ensureSessionId(env) {
  const got = await env.storageSessionGet(SESSION_ID_KEY);
  let sid = got && got[SESSION_ID_KEY];
  if (!sid) {
    sid = env.randomUUID();
    await env.storageSessionSet({ [SESSION_ID_KEY]: sid });
  }
  return sid;
}

export class Connection {
  // `buildSnapshot(now, sessionId)` is injected so this module does not import
  // the map/snapshot directly under test; the SW wires the real one. `onCommand`
  // is the NEXT-phase hook (default: log and ignore).
  constructor(env, { buildSnapshot, onCommand } = {}) {
    this.env = env;
    this.buildSnapshot = buildSnapshot;
    this.onCommand = onCommand || ((frame) => env.log("command (ignored, next phase):", frame && frame.command));
    this.ws = null;
    this.config = null;
    this.installUuid = null;
    this.sessionId = null;
    this.helloAcked = false;
  }

  // One-time-per-start init: config + ids. Safe to call repeatedly (idempotent).
  async init() {
    if (!this.config) this.config = await this.env.getInstanceConfig();
    if (!this.installUuid) this.installUuid = await ensureInstallUuid(this.env);
    if (!this.sessionId) this.sessionId = await ensureSessionId(this.env);
  }

  // Whether a socket is currently open (CONNECTING or OPEN counts as "in flight").
  _socketLive() {
    return (
      this.ws &&
      (this.ws.readyState === 0 /* CONNECTING */ ||
        this.ws.readyState === 1) /* OPEN */
    );
  }

  // The alarm handler: unconditionally ensure an open socket (§6). Independent of
  // in-memory state — after a worker resurrection `this.ws` is null and we
  // reconnect from scratch.
  async ensureSocket() {
    await this.init();
    if (this._socketLive()) return;
    this.connect();
  }

  connect() {
    const base = String(this.config.serviceUrl).replace(/\/+$/, "");
    const url = base + "/ext";
    this.helloAcked = false;
    const ws = new this.env.WebSocketImpl(url);
    this.ws = ws;
    ws.onopen = () => this._sendHello();
    // _onMessage is async (snapshot_request builds a snapshot); catch its
    // rejection so a failed buildSnapshot/tabs.query surfaces instead of becoming
    // an unhandled rejection that silently drops the reply.
    ws.onmessage = (event) => {
      this._onMessage(event.data).catch((e) =>
        this.env.log("message handler failed:", e),
      );
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      // Reconnect is the alarm's job; nothing scheduled here on purpose.
    };
    ws.onerror = () => {
      // Surfaced by onclose; nothing to do here besides not crashing the worker.
    };
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  _sendHello() {
    this._send({
      type: "hello",
      protocolVersion: PROTOCOL_VERSION,
      token: this.config.token,
      instanceId: this.config.instanceId,
      installUuid: this.installUuid,
      origin: this.env.origin || (typeof location !== "undefined" ? location.origin : undefined),
      title: this.config.title,
      sessionId: this.sessionId,
      // execute_js is gated by the options page (a later phase); default off.
      allowExecuteJs: !!this.config.allowExecuteJs,
    });
  }

  // Frames are JSON; correlation is by `id` (§6). Returns the frame handled (for
  // tests) or null.
  async _onMessage(raw) {
    let msg;
    try {
      msg = typeof raw === "string" ? JSON.parse(raw) : raw;
    } catch {
      return null;
    }
    const type = msg && msg.type;
    switch (type) {
      case "hello_ack":
        if (msg.ok) {
          // Ack-gated (§6): connection is "up" ONLY here, never on socket open.
          this.helloAcked = true;
        } else {
          this.env.log("hello rejected:", msg.error && msg.error.code);
          this.helloAcked = false;
        }
        return msg;
      case "snapshot_request": {
        const snap = await this.buildSnapshot(this.env.now(), this.sessionId);
        this._send({
          type: "snapshot",
          id: msg.id,
          sessionId: snap.sessionId,
          focusedWindowId: snap.focusedWindowId,
          tabs: snap.tabs,
          windows: snap.windows,
        });
        return msg;
      }
      case "ping":
        // pong is the only unsolicited-ish client message and is required (§6).
        this._send({ type: "pong" });
        return msg;
      case "command":
        // NEXT PHASE: verbs (open_tab/close_tab/execute_js) are executed here and
        // answered with {type:'response', id, ok, result|error}. For now, hand to
        // the hook and do NOT act.
        this.onCommand(msg);
        return msg;
      default:
        return null; // unknown/late frame — ignore (§6)
    }
  }
}
