// MV3 service worker entry (type: module). Wires chrome events to the activity
// map (§5) and drives the /ext connection (§6), which executes service commands
// through the §6 dispatcher (src/commands.js).
//
// The SW may die and be resurrected at any time; nothing here caches state in
// worker memory across an await that it could not rebuild — the activity map is
// in chrome.storage.session and the connection is re-established by the alarm.

import {
  onCreated,
  onActivated,
  onFocusChanged,
  onTick,
  onDocumentChange,
  onReplaced,
  onRemoved,
} from "./activity-map.js";
import { buildSnapshot } from "./snapshot.js";
import { chromeEnv, Connection } from "./connection.js";
import { handleDebuggerDetach } from "./commands.js";
import { enqueueOp, flushQueue } from "./quicklinks.js";
import {
  TICK_MS,
  RECONNECT_ALARM,
  TICK_ALARM,
  INSTANCE_ID_KEY,
} from "./constants.js";

const now = () => Date.now();

// Map mutations are fire-and-forget from a listener's view. The chain itself is
// poison-proof (a rejected link cannot break later links), but the per-call
// promise still needs a .catch — otherwise a storage failure becomes an unhandled
// rejection that silently drops the mutation and hides the fault.
const logFail = (p) => {
  if (p && typeof p.catch === "function") {
    p.catch((e) => console.error("[ext] activity map op failed:", e));
  }
};

// The Connection defaults its commandHandler to the real §6 dispatcher
// (src/commands.js): a `command` frame is executed against chrome.tabs/windows/
// scripting + the activity map and answered with a `response` frame.
const connection = new Connection(chromeEnv(), { buildSnapshot });

// Quick-links queue env (§6/§10): the SW owns the durable offline op queue and its
// flush. The credential moved off instance.json onto the SW (§7): the `/api/*` Bearer
// is now the RAW instance secret (slice C / option A — the server hashes it), and the
// address is the operator setting in storage.local. quicklinks.js still reads
// `config.serviceUrl` + `config.token`, so we synthesize that shape from the
// connection's resolved address + raw secret.
function quickLinksEnv() {
  return {
    getInstanceConfig: async () => {
      await connection.init();
      return {
        serviceUrl: await connection._resolveAddress(),
        token: await connection._apiSecret(), // slice C: the /api credential IS the raw secret
      };
    },
    storageLocalGet: (key) => chrome.storage.local.get(key),
    storageLocalSet: (obj) => chrome.storage.local.set(obj),
    fetchFn: (...a) => fetch(...a),
    randomUUID: () => crypto.randomUUID(),
    now: () => Date.now(),
  };
}

// --- tab / window activity events (§5 table) -------------------------------

chrome.tabs.onCreated.addListener((tab) => {
  logFail(onCreated(tab.id, now()));
});

chrome.tabs.onActivated.addListener((info) => {
  logFail(onActivated(info.tabId, info.windowId, now()));
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  logFail(onFocusChanged(windowId, now()));
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // Only a URL change can be a document change; the map link decides whether it
  // is a real origin+path change (activity) or query/fragment churn (not).
  if (changeInfo.url) logFail(onDocumentChange(tabId, changeInfo.url, now()));
});

chrome.tabs.onReplaced.addListener((addedTabId, removedTabId) => {
  logFail(onReplaced(addedTabId, removedTabId, now()));
});

chrome.tabs.onRemoved.addListener((tabId) => {
  logFail(onRemoved(tabId));
});

// --- chrome.debugger detach cleanup (§12, set_focus_emulation) --------------
// Registered ONCE here so the module-level attached-tabs set in commands.js stays honest
// when the debugger detaches for a reason outside the verb — the human closed the tab
// (`target_closed`) or opened DevTools on it (`canceled_by_user`). Without it a stale entry
// would make a later enable skip its attach and the sendCommand throw. `onDetach` is optional
// in some builds/tests, so guard the registration.
if (chrome.debugger && chrome.debugger.onDetach) {
  chrome.debugger.onDetach.addListener((source, _reason) => handleDebuggerDetach(source));
}

// --- alarms: reconnect + tick ----------------------------------------------

// ensureSocket() reads instance.json and touches storage: a malformed bundle config
// rejects it. Log the reason — a floating promise would surface as an unhandled
// rejection with no context, once per alarm, and the real fault (a broken
// instance.json) would never be named.
const ensureSocket = () =>
  connection.ensureSocket().catch((e) =>
    console.error("[ext] ensureSocket failed (check instance.json):", e),
  );

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) {
    ensureSocket();
  } else if (alarm.name === TICK_ALARM) {
    logFail(onTick(now()));
    // Opportunistically drain the quick-links queue: an op enqueued offline flushes
    // once connectivity returns, without waiting for the next enqueue (§10).
    flushQueue(quickLinksEnv()).catch((e) =>
      console.error("[ext] quick-links flush failed:", e),
    );
  }
});

// Register the alarms on install and on every worker start. Period 60 s (NOT 30 s):
// at 60 s the worker actually dies between ticks and the alarm resurrection is
// exercised (§6).
//
// CREATE ONLY WHAT IS MISSING. chrome.alarms.create on an EXISTING alarm resets its
// phase — the next fire is pushed a full period out. A self-navigating tab (§5, the
// Grafana playlist) wakes the worker every ~35–55 s and the worker dies in between,
// so every cold start would re-arm a 60 s alarm that never gets to fire: the tick
// stops happening at all, the watched tab ages unrecorded and the opportunistic
// quick-links flush starves.
async function ensureAlarms() {
  for (const name of [RECONNECT_ALARM, TICK_ALARM]) {
    const existing = await chrome.alarms.get(name);
    if (!existing) {
      chrome.alarms.create(name, { periodInMinutes: TICK_MS / 60000 });
    }
  }
}

const ensureAlarmsSafely = () =>
  ensureAlarms().catch((e) => console.error("[ext] alarm registration failed:", e));

chrome.runtime.onInstalled.addListener(ensureAlarmsSafely);
chrome.runtime.onStartup.addListener(ensureAlarmsSafely);

// Connect immediately on worker start; the alarm keeps it alive afterwards.
ensureAlarmsSafely();
ensureSocket();

// Revive a STRANDED claimed quick-links batch (§10): if the worker died mid-POST, the
// batch sits in storage marked claimed and nothing else would ever resend it — the
// next enqueue only appends. flushQueue re-sends it verbatim, under its ORIGINAL
// Idempotency-Key, so a POST that actually landed is a server-side no-op.
flushQueue(quickLinksEnv()).catch((e) =>
  console.error("[ext] quick-links flush on start failed:", e),
);

// --- internal page <-> SW interface (§6) -----------------------------------
// The startpage/options talk to the SW via runtime.sendMessage: identity,
// connection state, and the quick-links op queue.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message !== "object") return false;
  if (message.type === "get_identity") {
    // Identity moved off instance.json (§7): the id is SERVER-assigned — learned from
    // `enroll_accepted` (and re-confirmed by every hello_ack) and stored durably, so a
    // cold worker still answers. There is no separate `title`: the id IS the name (§6),
    // which is why the enrolled id is the only thing reported here. Until this browser
    // enrols, the locally-typed name is what it WANTS to be called; it is deliberately
    // not returned as an identity, because nothing has agreed to it yet.
    connection
      .init()
      .then(async () => {
        const idGot = await chrome.storage.local.get(INSTANCE_ID_KEY);
        sendResponse({ instanceId: (idGot && idGot[INSTANCE_ID_KEY]) || null });
      })
      .catch((e) => {
        console.error("[ext] get_identity failed:", e);
        sendResponse({ instanceId: null });
      });
    return true; // async response
  }
  if (message.type === "get_credential") {
    // The startpage/popup ask the SW for the /api base + Bearer (§7): the address
    // setting + the RAW instance secret (slice C / option A). The raw secret crosses only
    // SW->page in-process, then the TLS'd /api call — the server hashes it on receipt.
    //
    // `addressError` rides along for the SAME reason get_connection_state carries it: the
    // TLS gate resolves a REFUSED address to null, so `serviceUrl: null` alone cannot tell
    // "never configured" from "you typed ws://host and we refused it". Without it the popup
    // told an operator who HAD filled the field that no address was configured — the one
    // message guaranteed not to lead them to the fix.
    connection
      .init()
      .then(async () => {
        const addr = await connection._addressState();
        sendResponse({
          serviceUrl: addr.address,
          addressError: addr.error,
          secret: await connection._apiSecret(),
        });
      })
      .catch((e) => {
        console.error("[ext] get_credential failed:", e);
        sendResponse({ serviceUrl: null, addressError: null, secret: null });
      });
    return true; // async response
  }
  if (message.type === "submit_enrollment") {
    // The operator entered a window code in the settings UI and pressed submit (§7):
    // generate the secret if needed, mark the request pending, and send the
    // enroll_request now. Approval is learned later by a successful hello (acc 5).
    connection
      .submitEnrollment(message.code)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => {
        console.error("[ext] submit_enrollment failed:", e);
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      });
    return true; // async response
  }
  if (message.type === "get_connection_state") {
    // Real values (§6): `connected` is ack-gated, `lastSeenAt` is when the service was
    // last heard from, `rejectReason` is the hello_ack rejection code. The last two
    // are read back from storage.session so a worker resurrected between events does
    // not report a healthy instance as never-connected.
    connection
      .getConnectionState()
      .then(sendResponse)
      .catch((e) => {
        console.error("[ext] get_connection_state failed:", e);
        sendResponse({ connected: false, lastSeenAt: null, rejectReason: null });
      });
    return true; // async response
  }
  if (message.type === "enqueue_quicklink_op") {
    // The startpage has already updated its own view optimistically; here the SW
    // makes the op durable (queue + optimistic CACHE edit) and best-effort flushes
    // it (§6/§10). {op, url?, title?, id?, order?}.
    const { type: _t, ...op } = message;
    const env = quickLinksEnv();
    enqueueOp(env, op)
      .then(() => flushQueue(env))
      .then((res) => sendResponse({ ok: true, flush: res }))
      .catch((e) => {
        console.error("[ext] enqueue_quicklink_op failed:", e);
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      });
    return true; // async response
  }
  return false;
});

export { connection };
