// Minimal chrome + fetch mocks for the store/App tests. No network, no real
// extension — every capability the startpage touches is faked and recorded.

export function makeChrome(opts = {}) {
  const tabs = opts.tabs || [];
  const local = { ...(opts.local || {}) };
  const messages = opts.messages || {}; // { type: value | (msg)=>value }
  const calls = {
    tabUpdate: [],
    winUpdate: [],
    tabRemove: [],
    sendMessage: [],
    storageSet: [],
  };

  const chrome = {
    runtime: {
      getURL: (p) => "chrome-extension://mock/" + p,
      sendMessage: async (msg) => {
        calls.sendMessage.push(msg);
        const h = messages[msg.type];
        return typeof h === "function" ? h(msg) : h ?? null;
      },
    },
    tabs: {
      query: async () => tabs.map((t) => ({ ...t })),
      update: async (id, props) => {
        calls.tabUpdate.push([id, props]);
      },
      remove: async (id) => {
        calls.tabRemove.push(id);
      },
      getCurrent: async () => opts.current || { id: 9999 },
    },
    windows: {
      update: async (id, props) => {
        calls.winUpdate.push([id, props]);
      },
    },
    storage: {
      local: {
        get: async (key) =>
          key in local ? { [key]: structuredClone(local[key]) } : {},
        set: async (obj) => {
          calls.storageSet.push(obj);
          for (const k of Object.keys(obj)) local[k] = structuredClone(obj[k]);
        },
      },
    },
  };
  return { chrome, calls, local };
}

// A fetch router keyed by URL substring. Each route is a value or a (url,opts)=>value
// returning { ok, status, json }. Records call counts per route name.
export function makeFetch(routes = {}) {
  const counts = {};
  const fetchFn = async (url, opts) => {
    const u = String(url);
    if (u.includes("instance.json")) {
      counts.instance = (counts.instance || 0) + 1;
      const cfg = routes.instance || { serviceUrl: "wss://host/", token: "tok" };
      return jsonResponse(200, typeof cfg === "function" ? cfg() : cfg);
    }
    if (u.includes("/api/state")) {
      counts.state = (counts.state || 0) + 1;
      const r = routes.state;
      if (r === undefined) throw new Error("network down");
      const v = typeof r === "function" ? r(counts.state) : r;
      if (v instanceof Error) throw v;
      return jsonResponse(v.status ?? 200, v.body ?? v);
    }
    if (u.includes("/api/focus")) {
      counts.focus = (counts.focus || 0) + 1;
      const r = routes.focus;
      const v = typeof r === "function" ? r(opts, counts.focus) : r;
      return jsonResponse(v.status ?? 200, v.body ?? { ok: true });
    }
    throw new Error("unrouted fetch: " + u);
  };
  return { fetchFn, counts };
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}
