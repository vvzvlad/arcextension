// Minimal chrome + fetch mocks for the store/App tests. No network, no real
// extension — every capability the startpage touches is faked and recorded.

// The /api base + Bearer come from the SERVICE WORKER only (§7): the validated address
// setting + the RAW instance secret. There is no instance.json credential in any bundle
// anymore (the shared token is gone), so an enrolled profile — the normal case these
// tests describe — is one that ANSWERS get_credential. It is therefore served by default
// here; a test that needs the un-enrolled / no-address case passes `credential: null`,
// and an explicit `messages.get_credential` still wins.
const DEFAULT_CREDENTIAL = { serviceUrl: "wss://host/", secret: "tok" };

// --- bookmark tree helpers (the mock keeps a real tree so edits are observable) ---
function walkNodes(nodes, visit) {
  for (const n of nodes || []) {
    visit(n);
    if (n.children) walkNodes(n.children, visit);
  }
}

function findNode(nodes, id) {
  let found = null;
  walkNodes(nodes, (n) => {
    if (String(n.id) === String(id)) found = n;
  });
  return found;
}

function dropNode(nodes, id) {
  for (let i = 0; i < (nodes || []).length; i += 1) {
    if (String(nodes[i].id) === String(id)) {
      nodes.splice(i, 1);
      return true;
    }
    if (nodes[i].children && dropNode(nodes[i].children, id)) return true;
  }
  return false;
}

export function makeChrome(opts = {}) {
  // `tabs` is MUTABLE per test (via env.setTabs) so a re-query after a failed jump can
  // return a different list — that is the whole point of the "tab closed since the
  // render" case.
  let tabs = opts.tabs || [];
  const local = { ...(opts.local || {}) };
  // chrome.bookmarks / chrome.history are OPTIONAL capabilities of the startpage
  // (§10): the two extra columns are local sources, and the page must still render
  // when they are absent — an older Chrome, or a manifest without the two
  // permissions. `withoutOptionalApis: true` reproduces exactly that, so the
  // offline-first promise is tested against a browser that offers less, not more.
  //
  // `opts.bookmarks` is the list of nodes UNDER chrome's unnamed root (folders carry
  // `children`, links carry `url`), mirroring what getTree() really answers.
  const bookmarkRoot = { id: "0", title: "", children: structuredClone(opts.bookmarks || []) };
  const historyItems = structuredClone(opts.history || []);
  let nextBookmarkId = 1000;
  const messages = {
    get_credential: "credential" in opts ? opts.credential : DEFAULT_CREDENTIAL,
    ...(opts.messages || {}), // { type: value | (msg)=>value }
  };
  const calls = {
    tabUpdate: [],
    winUpdate: [],
    tabRemove: [],
    sendMessage: [],
    storageSet: [],
    bookmarkCreate: [],
    bookmarkUpdate: [],
    bookmarkRemove: [],
    bookmarkGetTree: 0,
    historySearch: [],
  };

  // chrome.events, faked with real add/removeListener bookkeeping: the point of the
  // bookmark-watch tests is that the page ATTACHES listeners and DETACHES them again on
  // unmount, so the mock has to be able to answer "how many are still attached".
  const makeEvent = () => {
    const listeners = [];
    return {
      listeners,
      addListener: (fn) => listeners.push(fn),
      removeListener: (fn) => {
        const i = listeners.indexOf(fn);
        if (i >= 0) listeners.splice(i, 1);
      },
      emit: (...args) => {
        for (const fn of [...listeners]) fn(...args);
      },
    };
  };
  const bookmarkEvents = {
    onCreated: makeEvent(),
    onChanged: makeEvent(),
    onRemoved: makeEvent(),
    onMoved: makeEvent(),
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
        // Real chrome.tabs.update REJECTS for a tab id that no longer exists.
        if (opts.tabUpdateThrowsFor != null && opts.tabUpdateThrowsFor === id) {
          throw new Error("No tab with id: " + id);
        }
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
        // structuredClone FIRST, exactly like the real API: a value that cannot be
        // cloned (e.g. a Vue reactive Proxy) must REJECT here rather than be silently
        // recorded — otherwise a broken write looks successful in tests and drops the
        // whole cache in the browser. `local` is the authoritative post-clone store;
        // assert against it, not against `calls.storageSet`.
        set: async (obj) => {
          const cloned = structuredClone(obj);
          calls.storageSet.push(cloned);
          for (const k of Object.keys(cloned)) local[k] = cloned[k];
        },
      },
    },
  };
  if (!opts.withoutOptionalApis) {
    chrome.bookmarks = {
      ...bookmarkEvents,
      getTree: async () => {
        calls.bookmarkGetTree += 1;
        return structuredClone([bookmarkRoot]);
      },
      create: async (node) => {
        calls.bookmarkCreate.push(node);
        if (opts.bookmarkWritesFail) throw new Error("bookmarks.create failed");
        const created = {
          id: String(nextBookmarkId++),
          parentId: node.parentId != null ? String(node.parentId) : "1",
          title: node.title || "",
          url: node.url,
        };
        const parent = findNode([bookmarkRoot], created.parentId) || bookmarkRoot;
        parent.children = parent.children || [];
        parent.children.push(created);
        return structuredClone(created);
      },
      update: async (id, changes) => {
        calls.bookmarkUpdate.push([id, changes]);
        if (opts.bookmarkWritesFail) throw new Error("bookmarks.update failed");
        const node = findNode([bookmarkRoot], id);
        if (!node) throw new Error("no bookmark " + id);
        Object.assign(node, changes);
        return structuredClone(node);
      },
      remove: async (id) => {
        calls.bookmarkRemove.push(id);
        if (opts.bookmarkWritesFail) throw new Error("bookmarks.remove failed");
        if (!dropNode(bookmarkRoot.children, id)) throw new Error("no bookmark " + id);
      },
    };
    chrome.history = {
      search: async (query = {}) => {
        calls.historySearch.push(query);
        const { text = "", startTime = 0, maxResults = 100 } = query;
        const needle = String(text).toLowerCase();
        return historyItems
          .filter((h) => (h.lastVisitTime ?? 0) >= startTime)
          .filter(
            (h) =>
              !needle ||
              String(h.title || "").toLowerCase().includes(needle) ||
              String(h.url || "").toLowerCase().includes(needle),
          )
          .slice(0, maxResults)
          .map((h) => ({ ...h }));
      },
    };
  }

  return {
    chrome,
    calls,
    local,
    bookmarkRoot,
    bookmarkEvents,
    // How many listeners the page currently holds on the bookmark tree. A leak test
    // asserts this is back to 0 after unmount.
    bookmarkListenerCount: () =>
      Object.values(bookmarkEvents).reduce((n, e) => n + e.listeners.length, 0),
    setTabs: (next) => {
      tabs = next;
    },
  };
}

// A fetch router keyed by URL substring. Each route is a value or a (url,opts)=>value
// returning { ok, status, json }. Records call counts per route name.
//
// A route function may be ASYNC and its result is awaited — that is how a test parks a
// request in flight (a slow server-side preview) and interleaves a UI action with it.
// Without the await such a route silently degrades to `{status: 200}` and any test
// built on the interleaving is vacuous.
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
      const v = await (typeof r === "function" ? r(counts.state) : r);
      if (v instanceof Error) throw v;
      return jsonResponse(v.status ?? 200, v.body ?? v);
    }
    if (u.includes("/api/focus")) {
      counts.focus = (counts.focus || 0) + 1;
      const r = routes.focus;
      const v = await (typeof r === "function" ? r(opts, counts.focus) : r);
      return jsonResponse(v.status ?? 200, v.body ?? { ok: true });
    }
    if (u.includes("/merge_windows")) {
      counts.merge = (counts.merge || 0) + 1;
      const r = routes.merge;
      const v = await (typeof r === "function" ? r(opts, counts.merge) : r);
      return jsonResponse((v && v.status) ?? 200, (v && v.body) ?? { merged: 0 });
    }
    if (u.includes("/api/pause")) {
      const method = (opts && opts.method ? opts.method : "POST").toUpperCase();
      if (method === "DELETE") {
        counts.pauseDelete = (counts.pauseDelete || 0) + 1;
        const r = routes.pauseDelete;
        const v = await (typeof r === "function" ? r(opts, counts.pauseDelete) : r);
        return jsonResponse((v && v.status) ?? 200, (v && v.body) ?? { resumed: true });
      }
      counts.pausePost = (counts.pausePost || 0) + 1;
      const r = routes.pausePost;
      const v = await (typeof r === "function" ? r(opts, counts.pausePost) : r);
      // POST /api/pause = indefinite stop (§7): the server answers with the stamp.
      return jsonResponse((v && v.status) ?? 200, (v && v.body) ?? { stopped_at: 1_000_000 });
    }
    // --- rules editor (§8/§10). /preview is more specific — check it first. -----
    if (u.includes("/api/rules/preview")) {
      counts.rulesPreview = (counts.rulesPreview || 0) + 1;
      const r = routes.rulesPreview;
      const v = await (typeof r === "function" ? r(opts, counts.rulesPreview) : r);
      return jsonResponse((v && v.status) ?? 200, (v && v.body) ?? { relocations: 0, closures: 0, impact: 0 });
    }
    if (u.includes("/api/rules")) {
      const method = (opts && opts.method ? opts.method : "GET").toUpperCase();
      if (method === "GET") {
        counts.rules = (counts.rules || 0) + 1;
        const r = routes.rules;
        if (r === undefined) return jsonResponse(200, { rules: [] });
        const v = await (typeof r === "function" ? r(counts.rules) : r);
        if (v instanceof Error) throw v;
        return jsonResponse((v && v.status) ?? 200, (v && v.body) ?? { rules: v.rules ?? [] });
      }
      counts.rulesSave = (counts.rulesSave || 0) + 1;
      const r = routes.rulesSave;
      const v = await (typeof r === "function" ? r(opts, counts.rulesSave) : r);
      return jsonResponse((v && v.status) ?? 200, (v && v.body) ?? { ok: true });
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
