// Hand-rolled in-memory chrome API mock.
//
// The point of this mock is that storage is TRULY async and whole-object
// last-write-wins, exactly like the real chrome.storage: get() and set() each
// resolve on a real macrotask (setTimeout 0), so two get/modify/set sequences
// that interleave lose an update UNLESS they are serialized through one promise
// chain. If get/set were synchronous the single-chain test would be vacuous.

// A tiny async gap on a real macrotask — enough for two independent async
// functions to interleave their get/await/set.
function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
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
  };

  const chrome = {
    storage: {
      session,
      local,
    },
    tabs: {
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
      getLastFocused: async () => {
        await tick();
        return { ...state.lastFocused };
      },
      onFocusChanged: new FakeEvent(),
    },
    alarms: {
      _alarms: {},
      create: (name, info) => {
        chrome.alarms._alarms[name] = info;
      },
      clear: (name) => {
        delete chrome.alarms._alarms[name];
      },
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
