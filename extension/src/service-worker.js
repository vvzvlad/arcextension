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
import { enqueueOp, flushQueue } from "./quicklinks.js";
import { TICK_MS, RECONNECT_ALARM, TICK_ALARM } from "./constants.js";

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
// flush. Injected deps mirror connection.js's chromeEnv so both sides read
// instance.json + storage.local the same way.
function quickLinksEnv() {
  return {
    getInstanceConfig: async () => {
      const resp = await fetch(chrome.runtime.getURL("instance.json"));
      return await resp.json();
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

// --- alarms: reconnect + tick ----------------------------------------------

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) {
    connection.ensureSocket();
  } else if (alarm.name === TICK_ALARM) {
    logFail(onTick(now()));
    // Opportunistically drain the quick-links queue: an op enqueued offline flushes
    // once connectivity returns, without waiting for the next enqueue (§10).
    flushQueue(quickLinksEnv()).catch((e) =>
      console.error("[ext] quick-links flush failed:", e),
    );
  }
});

// Register the alarms on install and on every worker start (idempotent). Period
// 60 s (NOT 30 s): at 60 s the worker actually dies between ticks and the alarm
// resurrection is exercised (§6).
function ensureAlarms() {
  chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: TICK_MS / 60000 });
  chrome.alarms.create(TICK_ALARM, { periodInMinutes: TICK_MS / 60000 });
}

chrome.runtime.onInstalled.addListener(ensureAlarms);
chrome.runtime.onStartup.addListener(ensureAlarms);

// Connect immediately on worker start; the alarm keeps it alive afterwards.
ensureAlarms();
connection.ensureSocket();

// --- internal page <-> SW interface (§6, minimal stub) ---------------------
// The startpage/options talk to the SW via runtime.sendMessage. Full surface is a
// later phase; expose identity + connection state so those pages can be built.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message !== "object") return false;
  if (message.type === "get_identity") {
    connection.init().then(() => {
      sendResponse({
        instanceId: connection.config && connection.config.instanceId,
        title: connection.config && connection.config.title,
      });
    });
    return true; // async response
  }
  if (message.type === "get_connection_state") {
    sendResponse({
      connected: connection.helloAcked,
      lastSeenAt: null, // populated in a later phase
      rejectReason: null,
    });
    return false;
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
