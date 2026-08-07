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
    scriptResults: opts.scriptResults || [{ result: null }], // scripting.executeScript return
  };

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
        return state.tabs.map((t) => ({ ...t }));
      },
      get: async (tabId) => {
        await tick();
        const t = state.tabs.find((x) => x.id === tabId);
        if (!t) throw new Error("no such tab");
        return { ...t };
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
        Object.assign(t, props);
        return { ...t };
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
      executeScript: async (_injection) => {
        await tick();
        return state.scriptResults;
      },
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
