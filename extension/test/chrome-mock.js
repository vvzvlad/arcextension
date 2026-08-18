// Hand-rolled in-memory chrome API mock.
//
// The point of this mock is that storage is TRULY async and whole-object
// last-write-wins, exactly like the real chrome.storage: get() and set() each
// resolve on a real macrotask (setTimeout 0), so two get/modify/set sequences
// that interleave lose an update UNLESS they are serialized through one promise
// chain. If get/set were synchronous the single-chain test would be vacuous.

// A tiny async gap on a real macrotask — enough for two independent async
// functions to interleave their get/await/set.
//
// Every gap is ACCOUNTED FOR: `pending` counts the operations that have been started
// but whose macrotask has not fired yet, so a test can drain the mock by STATE ("no
// unserviced operation is left") instead of by wall clock. That distinction is the
// whole point of the counter. The opening frame of an enrollment path costs 5-7
// chained storage round-trips, i.e. 5-7 real macrotasks; on a loaded machine (three
// vitest processes on two cores) they do not all fit inside any fixed timeout short
// enough to be worth writing, so a `setTimeout(25)`-style flush asserted on a
// half-written chain and the suite failed in a different place on every run. A
// time-based flush can only ever be tuned, never made correct.
let pending = 0;

function tick() {
  pending += 1;
  return new Promise((resolve) =>
    setTimeout(() => {
      // Decrement BEFORE resolving: the awaiting continuation runs as a microtask off
      // this resolve and may start the next operation, which must be counted afresh.
      pending -= 1;
      resolve();
    }, 0),
  );
}

// How many mock operations are in flight right now. Module-level on purpose: it covers
// every mock built in this test file, including a leftover write from the previous test
// still on its way to storage.
export function pendingOps() {
  return pending;
}

// Run the event loop until the mock has nothing left to serve, then return.
//
// Termination is decided by the QUEUE, not by elapsed time: each round yields one
// macrotask, which lets every already-expired mock timer fire and — because the
// microtask queue is drained after each timer callback — lets every continuation those
// unblocked either finish or start its next operation. Seeing `pending === 0` on two
// consecutive rounds therefore means the chain is genuinely done, on a fast laptop and
// on a starved CI runner alike.
//
// `maxRounds` is a stuck-detector, not a timeout to tune: real chains settle in well
// under twenty rounds, so hitting the cap means an await that will never be satisfied.
// Failing loudly there beats hanging until vitest's own timeout says only "5000ms".
export async function settle({ maxRounds = 1000 } = {}) {
  let quiet = 0;
  for (let round = 0; round < maxRounds; round += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    quiet = pending === 0 ? quiet + 1 : 0;
    if (quiet >= 2) return round + 1;
  }
  throw new Error(
    `chrome mock never went quiet: ${pending} operation(s) still in flight after ` +
      `${maxRounds} macrotasks — that is a stuck await, not a slow machine`,
  );
}

class AsyncStorageArea {
  constructor() {
    this.data = {};
  }

  // Real chrome.storage.get(key) resolves to an object { [key]: value }. Supports
  // a string key (the only form the code uses); returns {} for a missing key.
  async get(key) {
    await tick();
    // Return a DEEP COPY so a caller mutating the returned object does not alias
    // the stored object (the real API structured-clones across the process
    // boundary; without this copy the "lose an update" race would not reproduce
    // because both chains would mutate the same live object).
    if (key in this.data) {
      return { [key]: structuredClone(this.data[key]) };
    }
    return {};
  }

  // set(obj) writes each provided key WHOLE ("last write wins" over the whole
  // value), resolving on a macrotask.
  async set(obj) {
    await tick();
    for (const k of Object.keys(obj)) {
      this.data[k] = structuredClone(obj[k]);
    }
  }

  async remove(key) {
    await tick();
    delete this.data[key];
  }

  async clear() {
    await tick();
    this.data = {};
  }
}

// A minimal event hub matching chrome.events.Event (addListener + a test-only
// _emit).
class FakeEvent {
  constructor() {
    this.listeners = [];
  }
  addListener(fn) {
    this.listeners.push(fn);
  }
  removeListener(fn) {
    this.listeners = this.listeners.filter((l) => l !== fn);
  }
  _emit(...args) {
    for (const l of this.listeners) l(...args);
  }
}

// Build a fresh chrome mock. Options let a test seed live tabs/windows and the
// focus/idle answers that the tick and snapshot build query.
export function createChromeMock(opts = {}) {
  const session = new AsyncStorageArea();
  const local = new AsyncStorageArea();

  // Mutable "live browser" state the test controls.
  const state = {
    tabs: opts.tabs ? [...opts.tabs] : [], // array of {id, windowId, url, title, ...}
    windows: opts.windows ? [...opts.windows] : [], // array of {id, type, state}
    lastFocused: opts.lastFocused || { id: -1, focused: false },
    idleState: opts.idleState || "active",
    nextTabId: opts.nextTabId || 1000, // id counter for tabs.create
    nextWindowId: opts.nextWindowId || 500, // id counter for windows.create
    createWindowError: opts.createWindowError || null, // when set, windows.create throws
    moveError: opts.moveError || null, // when set, tabs.move throws this message
    removeError: opts.removeError || null, // when set, tabs.remove throws this message
    reloadError: opts.reloadError || null, // when set, tabs.reload throws this message (wake_tab)
    // wake_tab (§6, #68): a reload un-discards the tab and drives it to `complete`. Left 0, the
    // reload lands `complete` at once (the collapsed default); set to N and `status` stays
    // `loading` until the Nth subsequent `tabs.get`, counted in GETS so it is deterministic under
    // the fake clock — the same discipline as `navCommitAfterGets`.
    reloadCompleteAfterGets: opts.reloadCompleteAfterGets || 0,
    debuggerAttachError: opts.debuggerAttachError || null, // when set, debugger.attach throws
    sendCommandError: opts.sendCommandError || null, // when set, debugger.sendCommand throws
    detachError: opts.detachError || null, // when set, debugger.detach throws
    scriptResults: opts.scriptResults || [{ result: null }], // scripting.executeScript return
    // ⚠️ DEFERRED COMMIT, opt-in. Real `tabs.update({url})` does NOT change `tab.url`:
    // until the navigation commits, `tabs.get` answers the PREVIOUS url and the target
    // waits in `tab.pendingUrl`. This mock changed `url` SYNCHRONOUSLY, which is exactly
    // why no test could see a probe reading the OLD document. Set `navCommitAfterGets` to
    // N and the commit lands on the Nth `tabs.get` — counted in GETS, not milliseconds, so
    // it stays deterministic under the tests' fake clock. `navCommitUrl` is the address the
    // commit actually lands on when it differs from the requested one (a redirect).
    // Left at 0 the old synchronous shortcut applies, so every existing test is untouched.
    navCommitAfterGets: opts.navCommitAfterGets || 0,
    navCommitUrl: opts.navCommitUrl || null,
    // ⚠️ COMMIT AND COMPLETION ARE TWO EVENTS, and collapsing them (as this mock did) makes
    // the ordinary real state — document committed, page still loading — INEXPRESSIBLE. A
    // test written against the collapsed model reads as though a gate opened at the commit
    // while in a browser it would open only at full load. Set `navCompleteAfterGets` to a
    // get count LATER than `navCommitAfterGets` and the tab spends the gap where a real one
    // does: new url, no `pendingUrl`, `status:'loading'`. Left unset it equals the commit,
    // i.e. exactly the old collapsed behaviour, so every existing test is untouched.
    navCompleteAfterGets: opts.navCompleteAfterGets || 0,
  };

  // Strip the deferred-commit bookkeeping from a tab before it leaves the mock: the real
  // API has no such keys, and a test asserting on a whole tab object must not see them.
  const tabView = ({
    __navGets, __commitAt, __completeAt, __commitUrl, __reloadGets, __reloadCompleteAt, ...view
  }) => view;

  const chrome = {
    storage: {
      session,
      local,
    },
    tabs: {
      // ⚠️ The query FILTER IS IGNORED — every call gets every tab. That is faithful
      // today because every call site under this mock passes `{}` (snapshot.js,
      // commands.js), but it is a silent-default trap for the future: add a filtered
      // query to src/ and the code under test will receive tabs it asked to exclude,
      // and a test asserting on the result would pass for the wrong reason. Implement
      // the filter here the moment a filtered call site appears.
      query: async (_query) => {
        await tick();
        return state.tabs.map(tabView);
      },
      get: async (tabId) => {
        await tick();
        const t = state.tabs.find((x) => x.id === tabId);
        if (!t) throw new Error("no such tab");
        // The deferred navigation advances here, counted in gets. On the COMMIT get the url
        // becomes the committed address and `pendingUrl` clears — the status stays
        // `loading`, because a committed document is not a finished one. On the COMPLETION
        // get (the same one unless the test asked for a gap) the status becomes `complete`.
        if (t.__navGets !== undefined) {
          t.__navGets += 1;
          if (t.__navGets === t.__commitAt) {
            t.url = t.__commitUrl;
            delete t.pendingUrl;
          }
          if (t.__navGets >= t.__completeAt) {
            t.status = "complete";
            delete t.__navGets;
            delete t.__commitAt;
            delete t.__completeAt;
            delete t.__commitUrl;
          }
        }
        // The deferred RELOAD advances here the same way (wake_tab, #68): after
        // `reloadCompleteAfterGets` gets the reloaded tab flips from `loading` to `complete`.
        if (t.__reloadGets !== undefined) {
          t.__reloadGets += 1;
          if (t.__reloadGets >= t.__reloadCompleteAt) {
            t.status = "complete";
            delete t.__reloadGets;
            delete t.__reloadCompleteAt;
          }
        }
        return tabView(t);
      },
      // create/remove/update/move mutate the "live browser" state so the command
      // dispatcher can be exercised end to end; each resolves on a macrotask.
      create: async (props) => {
        await tick();
        const id = state.nextTabId++;
        const tab = {
          id,
          windowId: props.windowId ?? (state.lastFocused && state.lastFocused.id) ?? 1,
          url: props.url,
          pinned: !!props.pinned,
          active: !!props.active,
          audible: false,
        };
        state.tabs.push(tab);
        return { ...tab };
      },
      remove: async (tabId) => {
        await tick();
        if (state.removeError) throw new Error(state.removeError);
        const i = state.tabs.findIndex((x) => x.id === tabId);
        if (i === -1) throw new Error("no such tab");
        state.tabs.splice(i, 1);
      },
      update: async (tabId, props) => {
        await tick();
        const t = state.tabs.find((x) => x.id === tabId);
        if (!t) throw new Error("no such tab");
        if (props.url !== undefined && state.navCommitAfterGets > 0) {
          const { url, ...rest } = props;
          Object.assign(t, rest); // everything BUT the url applies at once, as it really does
          t.pendingUrl = url;
          t.status = "loading";
          t.__commitUrl = state.navCommitUrl || url;
          t.__navGets = 0;
          t.__commitAt = state.navCommitAfterGets;
          // A completion earlier than the commit is not a state a browser can be in, so an
          // unset (or nonsensical) value means "the same get", the collapsed default.
          t.__completeAt = Math.max(state.navCompleteAfterGets, state.navCommitAfterGets);
          return tabView(t);
        }
        Object.assign(t, props);
        return tabView(t);
      },
      // wake_tab (§6, #68): reload un-discards the tab and starts it loading. REJECTS "no such
      // tab" for a vanished target (wake_tab maps that to no_such_tab). With
      // `reloadCompleteAfterGets` unset the tab lands `complete` at once; set, it stays `loading`
      // until that many subsequent gets, so a test can observe the wait.
      reload: async (tabId) => {
        await tick();
        if (state.reloadError) throw new Error(state.reloadError);
        const t = state.tabs.find((x) => x.id === tabId);
        if (!t) throw new Error(`No tab with id: ${tabId}.`);
        t.discarded = false; // a reload re-materialises a discarded tab
        if (state.reloadCompleteAfterGets > 0) {
          t.status = "loading";
          t.__reloadGets = 0;
          t.__reloadCompleteAt = state.reloadCompleteAfterGets;
        } else {
          t.status = "complete";
        }
      },
      move: async (tabIds, moveProps) => {
        await tick();
        if (state.moveError) throw new Error(state.moveError);
        const ids = Array.isArray(tabIds) ? tabIds : [tabIds];
        for (const id of ids) {
          const t = state.tabs.find((x) => x.id === id);
          if (t) t.windowId = moveProps.windowId;
        }
      },
      onCreated: new FakeEvent(),
      onActivated: new FakeEvent(),
      onUpdated: new FakeEvent(),
      onReplaced: new FakeEvent(),
      onRemoved: new FakeEvent(),
    },
    windows: {
      getAll: async () => {
        await tick();
        return state.windows.map((w) => ({ ...w }));
      },
      // chrome.windows.get(id) resolves to the Window or REJECTS ("No window with id: N")
      // for a missing one — exactly what focus_window's existence guard relies on (§6).
      get: async (windowId) => {
        await tick();
        const w = state.windows.find((x) => x.id === windowId);
        if (!w) throw new Error(`No window with id: ${windowId}.`);
        return { ...w };
      },
      // windows.create resolves to the created Window WITH its `tabs` array — that is
      // how open_tab learns the id of the tab it just opened in a fresh window (§9,
      // the "browser with zero normal windows" branch).
      create: async (props = {}) => {
        await tick();
        if (state.createWindowError) throw new Error(state.createWindowError);
        const id = state.nextWindowId++;
        const win = {
          id,
          type: "normal",
          state: props.state || "normal",
          focused: !!props.focused,
        };
        // windows.create({tabId}) MOVES an existing tab into the new window (#45's
        // move_tab extract-to-new path) — no new tab id. It goes down the same Chromium
        // cross-window path as tabs.move, so it strips `pinned`, and throws "No tab with
        // id" for a tab that vanished. Modelled here so the command can be exercised end
        // to end.
        if (props.tabId !== undefined && props.tabId !== null) {
          const moved = state.tabs.find((t) => t.id === props.tabId);
          if (!moved) throw new Error(`No tab with id: ${props.tabId}.`);
          moved.windowId = id;
          moved.pinned = false; // cross-window create resets pinned
          moved.index = 0; // sole tab of the fresh window
          state.windows.push(win);
          return { ...win, tabs: [{ ...moved }] };
        }
        state.windows.push(win);
        const tab = props.url
          ? {
              id: state.nextTabId++,
              windowId: id,
              url: props.url,
              pinned: false,
              active: true,
              audible: false,
            }
          : null;
        if (tab) state.tabs.push(tab);
        return { ...win, tabs: tab ? [{ ...tab }] : [] };
      },
      getLastFocused: async () => {
        await tick();
        return { ...state.lastFocused };
      },
      update: async (windowId, props) => {
        await tick();
        const w = state.windows.find((x) => x.id === windowId);
        if (w) Object.assign(w, props);
        return w ? { ...w } : { id: windowId, ...props };
      },
      onFocusChanged: new FakeEvent(),
    },
    scripting: {
      // The injected `func` is NOT run — the canned `scriptResults` stands in for whatever
      // it would have returned (the injected bodies are unit-tested directly against a
      // document double instead). What IS modelled is the REJECTION for a target that no
      // longer exists: Chromium answers "No tab with id: N", and the polling verbs rely on
      // that rejection to end a wait on a tab the human closed rather than burning the
      // whole budget in silence.
      executeScript: async (injection) => {
        await tick();
        const tabId = injection && injection.target && injection.target.tabId;
        if (!state.tabs.some((t) => t.id === tabId)) {
          throw new Error(`No tab with id: ${tabId}.`);
        }
        return state.scriptResults;
      },
    },
    // chrome.debugger (§12, set_focus_emulation). Models the ONE-client-per-tab rule and
    // the promise shapes the verb relies on: attach REJECTS when `debuggerAttachError` is
    // set (DevTools open / another client), sendCommand/detach resolve. `_attached` lets a
    // test assert on the live attachment set, and `onDetach._emit(source, reason)` drives
    // the cleanup-listener test.
    debugger: {
      _attached: new Set(),
      attach: async ({ tabId }, _version) => {
        await tick();
        if (state.debuggerAttachError) throw new Error(state.debuggerAttachError);
        chrome.debugger._attached.add(tabId);
      },
      sendCommand: async ({ tabId }, _method, _params) => {
        await tick();
        if (state.sendCommandError) throw new Error(state.sendCommandError);
        return {};
      },
      detach: async ({ tabId }) => {
        await tick();
        if (state.detachError) throw new Error(state.detachError);
        chrome.debugger._attached.delete(tabId);
      },
      onDetach: new FakeEvent(),
      // WebSocket-frame capture (§12, wave 21) drives `Network.*` events into the buffer via
      // this hub. Tests call `handleDebuggerEvent` directly, but the service worker registers a
      // listener here on import, so the hub must exist. `onEvent._emit(source, method, params)`
      // is available for any test that wants to drive the registered path end-to-end.
      onEvent: new FakeEvent(),
    },
    alarms: {
      _alarms: { ...(opts.alarms || {}) },
      create: (name, info) => {
        // Record the CREATION ORDER too: re-creating an existing alarm resets its
        // phase in the real API, so a test must be able to see a redundant create.
        chrome.alarms._created.push(name);
        chrome.alarms._alarms[name] = info;
      },
      // MV3 promise form: resolves to the alarm or undefined when there is none.
      get: async (name) => {
        await tick();
        return chrome.alarms._alarms[name];
      },
      clear: (name) => {
        delete chrome.alarms._alarms[name];
      },
      _created: [],
      onAlarm: new FakeEvent(),
    },
    idle: {
      queryState: async (_seconds) => {
        await tick();
        return state.idleState;
      },
    },
    runtime: {
      getURL: (path) => `chrome-extension://mock-id/${path}`,
      // The running bundle's manifest — hello reports its `version` as `extVersion` so an
      // agent can tell a copy that predates a verb from one that has it.
      getManifest: () => ({ version: opts.extVersion || "9.9.9" }),
      onInstalled: new FakeEvent(),
      onStartup: new FakeEvent(),
      onMessage: new FakeEvent(),
    },
  };

  // Expose the mutable state so tests can flip focus/idle and mutate the live
  // tab/window set between calls.
  chrome.__state = state;
  return chrome;
}

// A minimal fake WebSocket driven by the test: the test plays the SERVICE side by
// calling _serverSend(...) and inspecting `sent`.
export class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
  }
  // Test triggers the open handshake.
  _open() {
    this.readyState = 1; // OPEN
    if (this.onopen) this.onopen();
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3; // CLOSED
    if (this.onclose) this.onclose();
  }
  // Test plays the service: deliver a frame to the client.
  _serverSend(obj) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
  }
}
