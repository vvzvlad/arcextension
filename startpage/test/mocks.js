// Minimal chrome + fetch mocks for the store/App tests. No network, no real
// extension — every capability the startpage touches is faked and recorded.

export function makeChrome(opts = {}) {
  // `tabs` is MUTABLE per test (via env.setTabs) so a re-query after a failed jump can
  // return a different list — that is the whole point of the "tab closed since the
  // render" case.
  let tabs = opts.tabs || [];
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
  return {
    chrome,
    calls,
    local,
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
      return jsonResponse(
        (v && v.status) ?? 200,
        (v && v.body) ?? { paused_until: 2_000_000, pause_started_at: 1_000_000 },
      );
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
