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
// is overridable in tests. The crypto seam (randomBytes) is here so vitest can stub it
// without Web Crypto in node.
export function chromeEnv() {
  return {
    // A bundle MAY still ship an instance.json — the ONLY shape it may have is
    // `{"serviceUrl": "wss://host", "title": "..."}`, both optional, both mere defaults
    // for the operator settings. It is NO LONGER the credential source: the shared
    // `token` and the self-reported `instanceId` are GONE (§7) and nothing reads them.
    // (This comment replaces the old instance.example.json, which was DELETED: it shipped
    // inside every bundle — the generator's copy filter skips `instance.json`, not
    // `*.example.json` — and described the REJECTED option B, "only its sha256 goes on
    // the wire", to the whole fleet.) Read best-effort; a missing/invalid file is fine.
    getInstanceConfig: async () => {
      try {
        const url = chrome.runtime.getURL("instance.json");
        const resp = await fetch(url);
        return await resp.json();
      } catch {
        return {};
      }
    },
    storageLocalGet: (key) => chrome.storage.local.get(key),
    storageLocalSet: (obj) => chrome.storage.local.set(obj),
    storageLocalRemove: (key) => chrome.storage.local.remove(key),
    storageSessionGet: (key) => chrome.storage.session.get(key),
    storageSessionSet: (obj) => chrome.storage.session.set(obj),
    randomUUID: () => crypto.randomUUID(),
    // 32 random bytes for the per-install secret (§7). Real Web Crypto in the browser.
    randomBytes: (n) => {
      const a = new Uint8Array(n);
      crypto.getRandomValues(a);
      return a;
    },
    // Which bundle is running, for the hello capability report. A seam like every other
    // chrome touch here so the connection tests can pin a version without a real manifest.
    manifestVersion: () => chrome.runtime.getManifest().version,
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
  INSTANCE_SECRET_KEY,
  INSTANCE_SECRET_PENDING_KEY,
  INSTANCE_ID_KEY,
  SERVICE_ADDRESS_KEY,
  INSTANCE_NAME_KEY,
  ENROLL_CODE_KEY,
  ENROLL_STATE_KEY,
  ENROLL_NEEDS,
  ENROLL_APPROVED,
  ENROLL_REVOKED,
  ENROLL_QUARANTINED,
  VERDICT_REVOKED,
  VERDICT_UNKNOWN,
} from "./constants.js";
import { normalizeServiceAddress, serviceAddressError } from "./service-address.js";
import { dispatchCommand } from "./commands.js";

// --- bytes -> hex (the 32-byte secret is generated once and stored/sent as hex) ----
// Option A: the RAW secret hex is what goes on the wire (over TLS) and is used as the
// /api Bearer; the server hashes it. The client never derives a sha256 anymore.
function bytesToHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// The durable enroll facts, with defaults for a never-touched profile.
//   requestPending: an enrolment was submitted and has not been ACCEPTED yet. It is not a
//     "waiting for an operator" flag anymore — there is nobody to wait for (§6) — only
//     "keep sending the enroll_request while a code is staged", which matters because the
//     MV3 worker can die between the operator's click and the socket opening.
//   quarantineProbe: a DURABLE 0/1/2 cursor cycling the quarantine opening frame across
//     cold-worker reconnects (0=enroll_request(pending), 1=hello(old), 2=hello(pending));
//     an in-memory cursor would reset to the same phase on every ~30 s worker death.
//   enrollReject: the last enroll_rejected reason (bad_code/closed/id_taken/bad_id/…),
//     surfaced via get_connection_state so the UI shows WHY instead of nothing.
// (`requestRegisteredAt` is gone with the `enroll_pending` frame: it timestamped the
// service's confirmation that it held a request awaiting approval, and no such state
// exists — the very next frame is the verdict.)
const DEFAULT_ENROLL_FACTS = {
  requestPending: false,
  approved: false,
  quarantined: false,
  lastVerdict: null,
  quarantineProbe: 0,
  enrollReject: null,
};

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
    this.serviceAddress = null; // the wss/ws URL (storage.local setting, §7)
    this.installUuid = null;
    this.sessionId = null;
    this.helloAcked = false;
    this._lastHelloWasPending = false; // which secret the last hello carried (§7 promote/discard)
    // Why the operator-submitted code is NOT held in memory: an MV3 worker dies every
    // ~30 s, so an in-memory one-shot code is lost precisely in the cases that need it
    // (the socket was not open at submit time, the service was down). It lives in the
    // DURABLE ENROLL_CODE_KEY and _sendOpening reads it from there.
    // §6 `get_connection_state` facts. In memory for the life of THIS worker and
    // mirrored into storage.session so a resurrected worker still reports them.
    this.lastSeenAt = null; // when the service was last heard from (any frame)
    this.rejectReason = null; // hello_ack{ok:false}.error.code, cleared by a good ack
    this._persistChain = Promise.resolve(); // orders the storage.session writes
  }

  // Read the persisted §6 facts, preferring what THIS worker observed. Never throws:
  // a storage failure degrades to "what we know in memory".
  //
  // `enrollState` and `hasAddress` are computed from DURABLE storage.local facts, NOT
  // from the in-memory helloAcked: at page open the MV3 worker is COLD, so an
  // ack-gated `connected` reads false and a healthy enrolled instance would render as
  // "no connection". Actual connectivity stays with /api/state on the startpage; this
  // branch is authoritative only for the enroll states (§7).
  async getConnectionState() {
    let persisted = {};
    try {
      const got = await this.env.storageSessionGet(CONNECTION_STATE_KEY);
      persisted = (got && got[CONNECTION_STATE_KEY]) || {};
    } catch (e) {
      this.env.log("reading connection state failed:", e);
    }
    let enrollState = ENROLL_NEEDS;
    let hasAddress = false;
    let addressError = null;
    let enrollReject = null;
    try {
      enrollState = await this.getEnrollState();
      // A REFUSED address (ws:// to a non-loopback host, an http:// site URL, a typo) is
      // reported separately from "not configured": both leave hasAddress false, but only
      // one of them is something the operator already typed and must be told about —
      // otherwise the UI says "адрес не настроен" over a field that visibly HAS a value.
      const addr = await this._addressState();
      hasAddress = !!addr.address;
      addressError = addr.error;
      // Durable so a COLD worker still surfaces the last enroll_rejected reason.
      enrollReject = (await this._loadEnrollFacts()).enrollReject || null;
    } catch (e) {
      this.env.log("reading enroll state failed:", e);
    }
    return {
      connected: !!this.helloAcked,
      lastSeenAt: this.lastSeenAt ?? persisted.lastSeenAt ?? null,
      rejectReason: this.rejectReason ?? persisted.rejectReason ?? null,
      enrollState,
      hasAddress,
      addressError,
      enrollReject,
    };
  }

  // --- durable enroll facts + secret helpers (§7) ---------------------------
  async _loadEnrollFacts() {
    try {
      const got = await this.env.storageLocalGet(ENROLL_STATE_KEY);
      return { ...DEFAULT_ENROLL_FACTS, ...((got && got[ENROLL_STATE_KEY]) || {}) };
    } catch (e) {
      this.env.log("reading enroll facts failed:", e);
      return { ...DEFAULT_ENROLL_FACTS };
    }
  }

  async _saveEnrollFacts(partial) {
    const facts = await this._loadEnrollFacts();
    await this.env.storageLocalSet({ [ENROLL_STATE_KEY]: { ...facts, ...partial } });
  }

  // Read a single storage.local string setting (address / browser name / code).
  async _readSetting(key) {
    const got = await this.env.storageLocalGet(key);
    const v = got && got[key];
    return typeof v === "string" && v.trim() ? v.trim() : null;
  }

  // The service address + the reason it was refused, if it was (§7). The operator
  // setting wins; a bundle's instance.json serviceUrl is only a bootstrap fallback (the
  // token/instanceId it used to carry are gone, §7).
  //
  // The TLS gate lives HERE, at the single place every consumer resolves the address
  // through — the /ext socket, the popup's and the startpage's /api Bearer, the
  // quick-links flush. A refused address resolves to null, so those consumers do not
  // "degrade to plaintext", they simply have nowhere to send the raw secret; and the
  // refusal is logged and reported through get_connection_state instead of being
  // invisible (see service-address.js for why an insecure address is not merely a
  // quality-of-service problem).
  async _addressState() {
    const raw =
      (await this._readSetting(SERVICE_ADDRESS_KEY)) ||
      (this.config && this.config.serviceUrl) ||
      null;
    if (!raw) return { address: null, error: null };
    // Normalize BEFORE judging and dial the normalized form: the operator may have
    // entered a bare `curator.example[:8443]` (the scheme is derivable, so the field
    // does not demand it — see service-address.js), and every consumer downstream
    // expects a real URL. An address that already carries a scheme is unchanged.
    const address = normalizeServiceAddress(raw);
    const error = serviceAddressError(address);
    if (error) {
      this.env.log("service address refused:", error, "—", raw);
      return { address: null, error };
    }
    return { address, error: null };
  }

  async _resolveAddress() {
    return (await this._addressState()).address;
  }

  async _readSecretHex(key) {
    const got = await this.env.storageLocalGet(key);
    const v = got && got[key];
    return typeof v === "string" && v ? v : null;
  }

  // The credential for the /api Bearer and the DEFAULT hello secret: the ACTIVE
  // (approved) secret. A pending re-enroll secret is deliberately NOT preferred here —
  // preferring it would (a) shadow the still-valid old secret on the /api Bearer and
  // (b) on the hello path leave a transiently-unknown instance forever helloing an
  // unenrolled secret (the fleet-brick this design exists to prevent). The pending
  // secret is reached EXPLICITLY by the quarantine probe in _sendOpening instead.
  async _activeSecretHex() {
    const active = await this._readSecretHex(INSTANCE_SECRET_KEY);
    if (active) return active;
    return await this._readSecretHex(INSTANCE_SECRET_PENDING_KEY);
  }

  // The RAW secret hex to use as the /api Bearer, or null when there is no secret
  // (needs-enroll). Option A: the client sends the raw secret (over TLS); the server
  // hashes it. This is the SAME value the hello frame carries.
  async _apiSecret() {
    return await this._activeSecretHex();
  }

  // Compute the enroll state from DURABLE facts only (§7). Order is load-bearing:
  // revoked (secret already wiped) → quarantined → approved → needs-enroll.
  //
  // There is no intermediate state between needs-enroll and approved: an enroll_request is
  // answered on the spot, so a browser is either enrolled or not. A generated-but-not-yet
  // -accepted secret used to read "pending"; it now reads needs-enroll, which is the truth
  // — the service has no row for it — and the last refusal REASON is reported alongside.
  async getEnrollState() {
    const facts = await this._loadEnrollFacts();
    const hasActive = !!(await this._readSecretHex(INSTANCE_SECRET_KEY));
    const hasPending = !!(await this._readSecretHex(INSTANCE_SECRET_PENDING_KEY));
    if (facts.lastVerdict === VERDICT_REVOKED && !hasActive && !hasPending) {
      return ENROLL_REVOKED;
    }
    if (facts.quarantined && hasActive) return ENROLL_QUARANTINED;
    if (facts.approved && hasActive) return ENROLL_APPROVED;
    return ENROLL_NEEDS;
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
    this.serviceAddress = await this._resolveAddress();
  }

  // Operator action (from the settings UI via the SW message channel): submit an
  // enrollment with the window `code`. Persists the code (so a cold worker / the
  // quarantine probe can reconstitute it), generates the secret to enroll, marks the
  // attempt pending (durable), and forces an immediate reconnect so the enroll_request
  // goes out now. The VERDICT arrives on that same socket — `enroll_accepted` (enrolled,
  // with the assigned id) or `enroll_rejected{reason}` — so there is no waiting state to
  // report and nothing to poll for.
  //
  // This call is NOT the moment the request reaches the service: the forced reconnect can
  // fail outright (service down, address refused) and the worker can die before the socket
  // opens. `requestPending` therefore stays set until an ACCEPT clears it, and _sendOpening
  // re-sends the request on every reconnect while a code is staged.
  async submitEnrollment(code) {
    await this.init();
    // No USABLE address (never set, or refused by the TLS gate) => there is nothing to
    // submit to. Fail loudly instead of arming the durable "pending" facts and letting
    // every surface report "ожидает одобрения" for a request that cannot be sent at all.
    if (!this.serviceAddress) {
      const { error } = await this._addressState();
      throw new Error(
        error
          ? "the configured service address was refused (" + error + ")"
          : "no service address is configured",
      );
    }
    if (code) await this.env.storageLocalSet({ [ENROLL_CODE_KEY]: code });
    const facts = await this._loadEnrollFacts();
    if (facts.quarantined) {
      // Re-enrolling a QUARANTINED instance: enroll a FRESH secret (the old one is the
      // very secret the server rejected as unknown), and keep the old one until the new
      // one is approved (_onApproved promotes it). The quarantine probe drives the frames.
      if (!(await this._readSecretHex(INSTANCE_SECRET_PENDING_KEY))) {
        const bytes = this.env.randomBytes(32);
        await this.env.storageLocalSet({ [INSTANCE_SECRET_PENDING_KEY]: bytesToHex(bytes) });
      }
      await this._saveEnrollFacts({ requestPending: true, quarantineProbe: 0, enrollReject: null });
    } else {
      // Initial enroll: generate the primary secret once if there is none.
      if (!(await this._readSecretHex(INSTANCE_SECRET_KEY))) {
        const bytes = this.env.randomBytes(32);
        await this.env.storageLocalSet({ [INSTANCE_SECRET_KEY]: bytesToHex(bytes) });
      }
      await this._saveEnrollFacts({
        requestPending: true,
        approved: false,
        lastVerdict: null,
        enrollReject: null,
      });
    }
    await this._forceReconnect();
  }

  // Tear down any live socket and (re)connect from scratch. The server closes the socket
  // on a failing hello_ack, and the 60 s alarm is too slow given the ~30 s MV3 worker
  // lifetime, so both the operator submit and the quarantine verdict drive this to send
  // their opening frame in the CURRENT live worker.
  async _forceReconnect() {
    if (this._socketLive()) {
      try {
        this.ws.close();
      } catch {
        /* best-effort */
      }
      this.ws = null;
    }
    await this.ensureSocket();
  }

  // Whether a socket is currently open (CONNECTING or OPEN counts as "in flight").
  _socketLive() {
    return (
      this.ws &&
      (this.ws.readyState === 0 /* CONNECTING */ ||
        this.ws.readyState === 1) /* OPEN */
    );
  }

  // The alarm handler: ensure an open socket when the instance has something to say
  // (§6/§7). Independent of in-memory state — after a worker resurrection `this.ws` is
  // null and we reconnect from scratch. Two gates before connecting:
  //   * no USABLE address — never configured, or configured and REFUSED by the TLS gate
  //     (_addressState): nothing to connect to (acc 13). An insecure address must not
  //     open a socket at all, because the very first frame would publish the secret;
  //   * NOTHING TO SEND — no secret at all. This covers both needs-enroll (never
  //     enrolled) AND revoked (secret already wiped): a revoked instance has hex===null,
  //     so _sendOpening would send nothing yet the socket would be held/reopened every
  //     alarm across the whole revoked fleet, forcing the server to accept and wait on an
  //     idle pre-auth socket for no reason. An enrolling instance always HAS a secret —
  //     submitEnrollment generates it before it reconnects — so a staged code needs no
  //     separate gate here.
  async ensureSocket() {
    await this.init();
    if (!this.serviceAddress) return;
    const hasSecret = (await this._activeSecretHex()) !== null;
    if (!hasSecret) return;
    if (this._socketLive()) return;
    this.connect();
  }

  connect() {
    const base = String(this.serviceAddress).replace(/\/+$/, "");
    const url = base + "/ext";
    this.helloAcked = false;
    const ws = new this.env.WebSocketImpl(url);
    this.ws = ws;
    // _sendOpening reads secret/enroll facts + the execute_js checkbox from storage
    // (async), so surface a rejected read instead of dropping the frame as an
    // unhandled rejection.
    ws.onopen = () =>
      this._sendOpening().catch((e) => this.env.log("opening frame send failed:", e));
    // _onMessage is async (snapshot_request builds a snapshot); catch its
    // rejection so a failed buildSnapshot/tabs.query surfaces instead of becoming
    // an unhandled rejection that silently drops the reply.
    //
    // Bind to THIS socket by identity, exactly like onclose below: a late frame from a
    // socket we have already preempted (e.g. a quarantine forced-reconnect swapped
    // `this.ws`) must not be processed against the CURRENT connection's in-memory state
    // (`_lastHelloWasPending`), or a stale hello_ack could drive a wrong promotion. The
    // no-wrong-promotion invariant must hold STRUCTURALLY, not just incidentally.
    ws.onmessage = (event) => {
      if (this.ws !== ws) return;
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

  // Decide the OPENING frame for a freshly opened socket (§7): an enroll_request while a
  // submitted request is not known to exist server-side, else a hello authenticated by
  // the RAW secret. Every branch reads DURABLE facts only — the worker that opens this
  // socket is usually not the one the operator clicked "submit" in.
  async _sendOpening() {
    const facts = await this._loadEnrollFacts();
    const hasPending = !!(await this._readSecretHex(INSTANCE_SECRET_PENDING_KEY));

    // Normal approved instance: hello with the active secret.
    if (facts.approved && !facts.quarantined) {
      await this._sendHello(INSTANCE_SECRET_KEY);
      return;
    }

    // QUARANTINE with a staged re-enroll (pending secret present): cycle a DURABLE probe
    // so BOTH secrets keep being attempted across cold-worker reconnects (invariants A+B):
    //   0 → enroll_request(pending, code): (re)register the re-enroll within the code TTL;
    //   1 → hello(old): a transient `unknown` that healed server-side recovers here with
    //       no re-enroll (invariant A — the old secret is NEVER shadowed off the hello);
    //   2 → hello(pending): detect the re-enrollment's approval (invariant B).
    // The code is reconstituted from the DURABLE ENROLL_CODE_KEY, so a cold worker still
    // sends the enroll_request, not a doomed hello.
    if (facts.quarantined && hasPending) {
      const probe = (facts.quarantineProbe || 0) % 3;
      await this._saveEnrollFacts({ quarantineProbe: (probe + 1) % 3 });
      const code = await this._readSetting(ENROLL_CODE_KEY);
      if (probe === 0 && code) {
        await this._sendEnrollRequest(INSTANCE_SECRET_PENDING_KEY, code);
        return;
      }
      if (probe === 2) {
        await this._sendHello(INSTANCE_SECRET_PENDING_KEY);
        return;
      }
      // probe === 1, or probe === 0 with an expired/cleared code: probe the OLD secret.
      await this._sendHello(INSTANCE_SECRET_KEY);
      return;
    }

    // INITIAL enrolment. While an attempt is staged and not yet accepted, the opening
    // frame IS the enroll_request: it is answered immediately, so re-sending it costs one
    // frame and is the only way a submit whose socket never opened (service down, worker
    // died before onopen) ever reaches the service. Nothing here polls — a code and an
    // un-accepted attempt are the whole condition.
    //
    // A staged code belongs to ONE window: `arm_enroll_window` (src/curator/enroll.py)
    // mints a FRESH code on every open, so once that window closes the code is dead and
    // the reply is enroll_rejected{closed} — which clears the code (below) and stops the
    // resend. Recovery is by design a manual step: the operator opens a new window and
    // types the new code on the options page.
    if (facts.requestPending && !facts.approved && !facts.quarantined) {
      const code = await this._readSetting(ENROLL_CODE_KEY);
      if (code) {
        await this._sendEnrollRequest(INSTANCE_SECRET_KEY, code);
        return;
      }
    }

    // Everything else (an enrolled instance, quarantine with no staged re-enroll, an
    // attempt whose code was cleared by a terminal refusal, …): hello with the active
    // secret to establish/keep the connection.
    await this._sendHello(INSTANCE_SECRET_KEY);
  }

  async _origin() {
    return (
      this.env.origin || (typeof location !== "undefined" ? location.origin : undefined)
    );
  }

  // The name this browser asks to be known by — and, on a successful enrol, IS known by:
  // the service takes it verbatim as the `instances` PRIMARY KEY (§6). A bundle
  // instance.json `title` stays a bootstrap fallback so a stamped build still has a
  // source, but it is subject to the same charset the service enforces.
  async _instanceName() {
    const setting = await this._readSetting(INSTANCE_NAME_KEY);
    return setting || (this.config && this.config.title) || undefined;
  }

  // enroll_request (§6) for the secret under `secretKey`: {type, protocolVersion,
  // installUuid, code, secret, instanceId}. Option A: the RAW secret hex goes on the wire
  // (over TLS) and the SERVER hashes it into the stored secret_hash.
  //
  // The name field is `instanceId` — ONE name for one thing. It used to be `title`, a
  // display name the operator then had to pair with an id typed in the console; there was
  // no rename anywhere in the product to pay for that split, so the two collapsed into the
  // id (§6). The frame no longer carries `origin` either: its only reader was the pending
  // row an operator inspected before approving, and both are gone.
  async _sendEnrollRequest(secretKey, code) {
    const secret = await this._readSecretHex(secretKey);
    this._send({
      type: "enroll_request",
      protocolVersion: PROTOCOL_VERSION,
      installUuid: this.installUuid,
      code,
      secret,
      instanceId: await this._instanceName(),
    });
  }

  // hello (§2/§7) authenticated by the secret under `secretKey`. The shared `token` is
  // GONE and the client no longer self-reports a trusted `instanceId` — the server
  // hashes the RAW secret and resolves the id from it (slice B / option A).
  // `_lastHelloWasPending` records WHICH secret this hello carried so _onApproved knows
  // whether a subsequent ok:true means "the re-enrolled secret was accepted" (promote) or
  // "the old secret recovered" (discard the moot re-enroll). A hello with no secret is a
  // no-op (nothing to say). The frame no longer carries a `title`: the id IS the name and
  // the service assigned it at enrolment — a hello cannot change it (§6).
  async _sendHello(secretKey = INSTANCE_SECRET_KEY) {
    const secret = await this._readSecretHex(secretKey);
    if (!secret) return; // needs-enroll: no secret to authenticate with
    this._lastHelloWasPending = secretKey === INSTANCE_SECRET_PENDING_KEY;
    this._send({
      type: "hello",
      protocolVersion: PROTOCOL_VERSION,
      secret,
      installUuid: this.installUuid,
      origin: await this._origin(),
      sessionId: this.sessionId,
      // The AUTHORITATIVE JS & Debugger state is the single options checkbox in
      // storage.local (§12) — a copied bundle sets its own checkbox. One switch now
      // gates both execute_js and the chrome.debugger path; the storage KEY keeps its
      // historical name `allowExecuteJs` (renaming it would reset every installed copy to
      // OFF). Report it.
      allowExecuteJs: await this._readAllowExecuteJs(),
      // The rest of the CAPABILITY REPORT (§11/§12). An agent must be able to see what
      // this copy is running BEFORE it calls and fails mid-task.
      //   extVersion — which bundle is actually running. The service and the extension
      //     update by DIFFERENT paths (the Dockerfile does not ship extension/), so
      //     "new service + old extension" is a guaranteed state, and until now nothing
      //     could tell an agent that the copy it is talking to predates a verb.
      extVersion: this._manifestVersion(),
    });
  }

  // The running bundle's manifest version. Best-effort and never fatal: a hello that
  // failed because the version could not be read would be a capability REPORT taking
  // the connection down, which is exactly backwards. `null` = "this copy did not say".
  _manifestVersion() {
    try {
      const v = this.env.manifestVersion();
      return typeof v === "string" && v ? v : null;
    } catch (e) {
      this.env.log("reading manifest version failed:", e);
      return null;
    }
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

  // The instance is enrolled and the secret in play is active (§7) — reached from a
  // successful hello_ack AND from `enroll_accepted`, which is the FIRST evidence a fresh
  // enrolment landed. Record the durable approved fact + the server-assigned id, and
  // resolve any pending re-enroll:
  //   * the PENDING secret was the one accepted (`promotePending`) → PROMOTE it and
  //     retire the old one (this overwrite IS the wipe — and only now, never eagerly);
  //   * the OLD secret succeeded (a transient `unknown` healed server-side) → the
  //     parallel re-enroll is moot: DISCARD the pending secret, keep the old one.
  async _onApproved(instanceId, promotePending = this._lastHelloWasPending) {
    const pending = await this._readSecretHex(INSTANCE_SECRET_PENDING_KEY);
    if (pending) {
      if (promotePending) {
        await this.env.storageLocalSet({ [INSTANCE_SECRET_KEY]: pending });
      }
      // Either way the pending slot is cleared: promoted into active, or dropped as moot.
      await this.env.storageLocalRemove(INSTANCE_SECRET_PENDING_KEY);
    }
    if (typeof instanceId === "string" && instanceId) {
      await this.env.storageLocalSet({ [INSTANCE_ID_KEY]: instanceId });
    }
    await this._saveEnrollFacts({
      approved: true,
      requestPending: false,
      quarantined: false,
      lastVerdict: null,
      quarantineProbe: 0,
      enrollReject: null,
    });
    // The one-time window code has served its purpose (or was moot). Clear it so a stale
    // code cannot silently re-arm a future quarantine's auto-re-enroll.
    await this.env.storageLocalRemove(ENROLL_CODE_KEY);
    this._lastHelloWasPending = false;
  }

  // hello_ack{ok:false} verdicts the client ACTS on (§7).
  async _onVerdict(code) {
    if (code === VERDICT_REVOKED) {
      // The operator revoked this instance: WIPE the secret and drop to needs-enroll,
      // so the only way back is a deliberate re-enrollment (§13/acc 8 tail). Keep the
      // durable lastVerdict='revoked' so the status bar can say "отозван" even though
      // the secret (and thus the connection) is gone.
      await this.env.storageLocalRemove(INSTANCE_SECRET_KEY);
      await this.env.storageLocalRemove(INSTANCE_SECRET_PENDING_KEY);
      await this.env.storageLocalRemove(INSTANCE_ID_KEY);
      await this._saveEnrollFacts({
        approved: false,
        requestPending: false,
        quarantined: false,
        lastVerdict: VERDICT_REVOKED,
      });
      return;
    }
    if (code === VERDICT_UNKNOWN) {
      const facts = await this._loadEnrollFacts();
      // Never enrolled => `unknown` just means this secret was never accepted (a submit
      // whose enroll_request has not landed yet, or a refused one). Nothing to quarantine;
      // the alarm keeps trying, and _sendOpening re-sends the enroll_request while a code
      // is staged.
      if (!facts.approved) return;
      // A PREVIOUSLY-APPROVED instance suddenly unknown => quarantine WITHOUT wiping.
      await this._quarantine();
      return;
    }
    // protocol / origin / auth / duplicate: nothing to storage — rejectReason already
    // records why for the status bar; the alarm keeps retrying.
  }

  // Quarantine (unknown_instance after approval, §7). We do NOT wipe the secret.
  //
  // RATIONALE (documented, mine): `unknown` is exactly what the server answers after a
  // restore from an OLD backup or a transient DB error — and it hits the WHOLE fleet at
  // once. The enrollment window code lives only ~10 min, so wiping the secret on
  // `unknown` and auto-enrolling would turn one bad restore into a fleet-wide
  // walk-through-the-door on a timer. Instead we keep the old secret (a transient
  // `unknown` that heals server-side is then recovered by a later successful hello with
  // no re-enroll, no code), and re-enroll only DELIBERATELY: a fresh secret is generated
  // under the PENDING key and enrolled, and the old secret is retired ONLY after that
  // fresh secret is approved (_onApproved promotes it). The parallel re-enroll is
  // attempted here only when the operator has already STAGED a window code — never
  // blindly — and is BEST-EFFORT: the code is single-use and lives only ~10 min, so if
  // it expires before an approval the probe's enroll_request just starts drawing
  // enroll_rejected(bad_code/closed), which is surfaced to the operator to re-stage.
  async _quarantine() {
    await this._saveEnrollFacts({
      quarantined: true,
      approved: false,
      lastVerdict: VERDICT_UNKNOWN,
      quarantineProbe: 0,
    });
    const code = await this._readSetting(ENROLL_CODE_KEY);
    if (code) {
      // Stage a fresh secret for the parallel re-enroll (keep the old one intact — the
      // hello probe keeps attempting it for a transient-unknown recovery).
      if (!(await this._readSecretHex(INSTANCE_SECRET_PENDING_KEY))) {
        const bytes = this.env.randomBytes(32);
        await this.env.storageLocalSet({
          [INSTANCE_SECRET_PENDING_KEY]: bytesToHex(bytes),
        });
      }
      await this._saveEnrollFacts({ requestPending: true });
    }
    // The server closed the socket on this failing hello_ack; force an immediate
    // reconnect (a live worker, not the 60 s alarm that outlives the ~30 s worker) so
    // the enroll_request / the next probe frame is attempted NOW. When no code is
    // staged, the probe simply keeps helloing the old secret for transient recovery.
    await this._forceReconnect();
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
          await this._onApproved(msg.instanceId);
        } else {
          const code = (msg.error && msg.error.code) || "rejected";
          this.env.log("hello rejected:", code);
          this.helloAcked = false;
          this.rejectReason = code;
          await this._onVerdict(code);
        }
        await this._persistConnectionState();
        return msg;
      case "enroll_accepted":
        // ENROLLED, right now — the window with a valid code was the whole permission
        // (§6), so this frame carries the assigned id and there is nothing further to
        // wait for. It replaced `enroll_pending`, which only ever meant "an operator has
        // yet to look at this".
        //
        // `promotePending: true` is passed explicitly rather than reusing
        // `_lastHelloWasPending`: this frame answers an enroll_request, not a hello, and
        // an enroll_request during quarantine carries the PENDING secret — that is the
        // secret just accepted, so it is the one to promote. Reading the hello flag here
        // would leave the freshly enrolled secret in the pending slot and keep the retired
        // one active.
        this.env.log("enrolled as", msg.instanceId);
        this.helloAcked = false; // an enrol is not a connection; the hello below makes one
        this.rejectReason = null;
        await this._onApproved(msg.instanceId, true);
        await this._persistConnectionState();
        // The service closes this socket right after the frame. Reconnect NOW so the hello
        // goes out in THIS live worker rather than waiting up to 60 s for the alarm — the
        // same reason submit and the quarantine verdict force a reconnect.
        await this._forceReconnect();
        return msg;
      case "enroll_rejected": {
        // Surface the reason (closed / bad_code / id_taken / bad_id / protocol) so the
        // operator can react. It is persisted in the DURABLE facts so get_connection_state
        // (read by a COLD worker at page open) shows WHY — this is now the ONLY place a
        // refusal is visible to a human, since there is no pending list in /admin for a
        // refused attempt to sit in.
        const reason = (msg && msg.reason) || "rejected";
        this.rejectReason = "enroll_" + reason;
        this.env.log("enroll rejected:", reason);
        // A refused request created NOTHING server-side (every reject path in
        // `_handle_enroll` returns before the write), so the attempt stays pending here
        // and the next opening frame re-sends it — unless its reason is terminal, below.
        await this._saveEnrollFacts({ enrollReject: reason });
        // TERMINAL reasons: nothing this client can retry will change the answer, so the
        // staged code is cleared and the resend STOPS until a human acts.
        //   * bad_code / closed (src/ext/protocol.py ENROLL_BAD_CODE / ENROLL_CLOSED) — a
        //     wrong or expired code, or a closed window: the code is dead for good, since
        //     every window mints a fresh one.
        //   * id_taken / bad_id — the code may well still be live, but the NAME is the
        //     problem, and re-sending the same frame draws the same refusal forever. The
        //     operator has to change the name in these settings and submit again (which
        //     re-stages the code from the still-filled field).
        // Retrying anything else (protocol skew) keeps the code so a retry can land.
        if (
          reason === "bad_code" ||
          reason === "closed" ||
          reason === "id_taken" ||
          reason === "bad_id"
        ) {
          await this.env.storageLocalRemove(ENROLL_CODE_KEY);
        }
        await this._persistConnectionState();
        return msg;
      }
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
