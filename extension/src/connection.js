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
  ALLOW_EXECUTE_JS_KEY,
  CONNECTION_STATE_KEY,
} from "./constants.js";
import { dispatchCommand } from "./commands.js";

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
  // the map/snapshot directly under test; the SW wires the real one.
  // `commandHandler(frame, ctx)` executes a `command` frame and returns
  // `{ok, result|error}`; it defaults to the real §6 dispatcher and is
  // overridable so the wiring can be tested in isolation.
  constructor(env, { buildSnapshot, commandHandler } = {}) {
    this.env = env;
    this.buildSnapshot = buildSnapshot;
    this.commandHandler = commandHandler || dispatchCommand;
    this.ws = null;
    this.config = null;
    this.installUuid = null;
    this.sessionId = null;
    this.helloAcked = false;
    // §6 `get_connection_state` facts. In memory for the life of THIS worker and
    // mirrored into storage.session so a resurrected worker still reports them.
    this.lastSeenAt = null; // when the service was last heard from (any frame)
    this.rejectReason = null; // hello_ack{ok:false}.error.code, cleared by a good ack
    this._persistChain = Promise.resolve(); // orders the storage.session writes
  }

  // Read the persisted §6 facts, preferring what THIS worker observed. Never throws:
  // a storage failure degrades to "what we know in memory".
  async getConnectionState() {
    let persisted = {};
    try {
      const got = await this.env.storageSessionGet(CONNECTION_STATE_KEY);
      persisted = (got && got[CONNECTION_STATE_KEY]) || {};
    } catch (e) {
      this.env.log("reading connection state failed:", e);
    }
    return {
      connected: !!this.helloAcked,
      lastSeenAt: this.lastSeenAt ?? persisted.lastSeenAt ?? null,
      rejectReason: this.rejectReason ?? persisted.rejectReason ?? null,
    };
  }

  // Persist the facts a resurrected worker cannot re-derive. Called on inbound frames
  // OTHER than `command`, not only on hello_ack: an MV3 worker dies between events, so
  // persisting only at ack time throws away every sighting after it — a worker
  // resurrected half an hour later reports the ack-time `lastSeenAt`, and the status
  // bar's "закрыт N назад" is wrong by that whole half hour (§10 tells "closed" from
  // "stale" by exactly this number).
  //
  // `command` frames are EXCLUDED because they are the hot path: a 200-tab pass is
  // hundreds of them, and a storage write in front of every dispatch buys nothing —
  // the 15 s heartbeat (`ping`) keeps the persisted value fresh to well within the
  // second §10 needs, right through the pass.
  //
  // The writes are CHAINED and snapshot their payload at call time: two frames handled
  // concurrently would otherwise race, and a late-landing ordinary frame could
  // overwrite a `rejectReason` written after it.
  _persistConnectionState() {
    const payload = {
      lastSeenAt: this.lastSeenAt,
      rejectReason: this.rejectReason,
    };
    this._persistChain = this._persistChain
      .then(() => this.env.storageSessionSet({ [CONNECTION_STATE_KEY]: payload }))
      .catch((e) => this.env.log("persisting connection state failed:", e));
    return this._persistChain;
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
    // _sendHello reads the execute_js checkbox from storage (async), so surface a
    // rejected read instead of dropping the hello as an unhandled rejection.
    ws.onopen = () =>
      this._sendHello().catch((e) => this.env.log("hello send failed:", e));
    // _onMessage is async (snapshot_request builds a snapshot); catch its
    // rejection so a failed buildSnapshot/tabs.query surfaces instead of becoming
    // an unhandled rejection that silently drops the reply.
    ws.onmessage = (event) => {
      this._onMessage(event.data).catch((e) =>
        this.env.log("message handler failed:", e),
      );
    };
    ws.onclose = () => {
      if (this.ws === ws) {
        this.ws = null;
        // A closed socket is NOT a connection (§6 `get_connection_state.connected`):
        // leaving helloAcked true would show a dead instance as "на связи" until the
        // next alarm. `lastSeenAt` deliberately stays — it is when we last heard from
        // the service, which is what makes "closed N ago" distinguishable from
        // "never seen" in the status bar (§10).
        this.helloAcked = false;
      }
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

  async _sendHello() {
    this._send({
      type: "hello",
      protocolVersion: PROTOCOL_VERSION,
      token: this.config.token,
      instanceId: this.config.instanceId,
      installUuid: this.installUuid,
      origin: this.env.origin || (typeof location !== "undefined" ? location.origin : undefined),
      title: this.config.title,
      sessionId: this.sessionId,
      // The AUTHORITATIVE execute_js state is the options checkbox in
      // storage.local (§12), not instance.json — a copied bundle shares the
      // config default but sets its own checkbox. Report the stored value.
      allowExecuteJs: await this._readAllowExecuteJs(),
    });
  }

  // Report EXACTLY what the execute_js gate enforces (§12: "инстанс сообщает
  // состояние галочки, дефолт выкл"). The gate in commands.js reads ONLY
  // storage.local and defaults OFF — it never consults instance.json — so hello
  // must mirror that, or the service would hold a false "can execute_js" state and
  // send a doomed execute_js. An unset key (or a read error) => OFF.
  async _readAllowExecuteJs() {
    try {
      const got = await this.env.storageLocalGet(ALLOW_EXECUTE_JS_KEY);
      return !!(got && got[ALLOW_EXECUTE_JS_KEY]);
    } catch (e) {
      this.env.log("reading execute_js checkbox failed:", e);
      return false;
    }
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
    // Any well-formed frame proves the service was alive just now: that is exactly what
    // `lastSeenAt` reports to the startpage (§6). Persisting is fire-and-forget (the
    // chain orders the writes and swallows failures) and SKIPPED for `command` — see
    // _persistConnectionState for why the hot path is excluded.
    this.lastSeenAt = this.env.now();
    if (type !== "command") this._persistConnectionState();
    switch (type) {
      case "hello_ack":
        if (msg.ok) {
          // Ack-gated (§6): connection is "up" ONLY here, never on socket open.
          this.helloAcked = true;
          this.rejectReason = null; // a good ack clears a previous rejection
        } else {
          this.env.log("hello rejected:", msg.error && msg.error.code);
          this.helloAcked = false;
          this.rejectReason = (msg.error && msg.error.code) || "rejected";
        }
        await this._persistConnectionState();
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
      case "command": {
        // Execute the verb (§6) and answer with a correlated `response`. The
        // handler owns the session check and every guard; it never throws (an
        // unexpected failure comes back as {ok:false, error:{code:'internal'}}),
        // but wrap defensively so a bug still yields a reply rather than a silent
        // drop that leaves the service awaiting forever.
        let outcome;
        try {
          outcome = await this.commandHandler(msg, {
            sessionId: this.sessionId,
            now: this.env.now,
          });
        } catch (e) {
          this.env.log("command handler crashed:", e);
          outcome = { ok: false, error: { code: "internal", message: String((e && e.message) || e) } };
        }
        this._send({ type: "response", id: msg.id, ...outcome });
        return msg;
      }
      default:
        return null; // unknown/late frame — ignore (§6)
    }
  }
}
