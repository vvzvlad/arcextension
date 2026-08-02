import { describe, it, expect, beforeEach, vi } from "vitest";
import { createChromeMock } from "./chrome-mock.js";
import { dispatchCommand } from "../src/commands.js";
import * as activityMap from "../src/activity-map.js";
import {
  CMD_OPEN_TAB,
  CMD_CLOSE_TAB,
  CMD_GET_TAB,
  CMD_FOCUS_TAB,
  CMD_NAVIGATE_TAB,
  CMD_MERGE_WINDOWS,
  CMD_EXECUTE_JS,
} from "../src/constants.js";

const NOW = 1_000_000_000;
const SID = "session-1";

// A fully-spied activity map so ordering / "not called" / "no leak" assertions
// are exact. readMap is configurable per test (default: an empty map).
function spyMap(readValue = { tabs: {} }) {
  return {
    seedCuratorTab: vi.fn(async () => {}),
    markCuratorCause: vi.fn(async () => {}),
    clearCuratorCause: vi.fn(async () => {}),
    readMap: vi.fn(async () => readValue),
  };
}

function ctx(overrides = {}) {
  return { sessionId: SID, now: () => NOW, map: spyMap(), ...overrides };
}

function frame(command, params = {}, sessionId = SID) {
  return { type: "command", id: "c1", sessionId, command, params };
}

beforeEach(() => {
  globalThis.chrome = createChromeMock();
  activityMap.__resetQueue();
});

// --- session check (§5): FIRST, before any verb executes --------------------
describe("stale_session rejects every verb without executing", () => {
  const verbs = [
    [CMD_OPEN_TAB, { url: "https://x/" }],
    [CMD_CLOSE_TAB, { tabId: 1, expect: {} }],
    [CMD_GET_TAB, { tabId: 1 }],
    [CMD_FOCUS_TAB, { tabId: 1 }],
    [CMD_NAVIGATE_TAB, { tabId: 1, url: "https://x/" }],
    [CMD_MERGE_WINDOWS, { windowIds: [2], targetWindowId: 1 }],
    [CMD_EXECUTE_JS, { code: "1", tabId: 1 }],
  ];

  it.each(verbs)("%s from a foreign session => stale_session, no side effects", async (cmd, params) => {
    const create = vi.spyOn(chrome.tabs, "create");
    const remove = vi.spyOn(chrome.tabs, "remove");
    const update = vi.spyOn(chrome.tabs, "update");
    const move = vi.spyOn(chrome.tabs, "move");
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const c = ctx();

    // frame.sessionId ("dead") != ctx.sessionId (SID) => must be rejected.
    const res = await dispatchCommand(frame(cmd, params, "dead-session"), c);

    expect(res).toEqual({ ok: false, error: { code: "stale_session", message: expect.any(String) } });
    // Nothing touched the browser or the map.
    for (const spy of [create, remove, update, move, exec]) expect(spy).not.toHaveBeenCalled();
    expect(c.map.seedCuratorTab).not.toHaveBeenCalled();
    expect(c.map.markCuratorCause).not.toHaveBeenCalled();
  });
});

// --- open_tab ---------------------------------------------------------------
describe("open_tab", () => {
  it("rejects javascript: and data: urls at the edge, tabs.create NOT called", async () => {
    for (const url of ["javascript:alert(1)", "data:text/html,<b>x", "chrome://settings", "file:///etc/passwd"]) {
      const create = vi.spyOn(chrome.tabs, "create");
      const c = ctx();
      const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url }), c);
      expect(res.ok).toBe(false);
      expect(res.error.code).toBe("precondition_failed");
      expect(create).not.toHaveBeenCalled(); // never reached tabs.create
      expect(c.map.seedCuratorTab).not.toHaveBeenCalled();
      create.mockRestore();
    }
  });

  it("accepts http/https, creates the tab and seeds the activity map (real map)", async () => {
    // Real map here: assert the record was actually seeded with the source ages.
    const res = await dispatchCommand(
      frame(CMD_OPEN_TAB, {
        url: "https://example.com/a",
        pinned: true,
        seed_age_ms: 5000,
        seed_opened_ago_ms: 9000,
      }),
      { sessionId: SID, now: () => NOW }, // no map override => real activityMap
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ tabId: 1000, windowId: expect.anything() });

    const map = await activityMap.readMap();
    const rec = map.tabs[1000];
    expect(rec).toBeDefined();
    expect(rec.lastActive).toBe(NOW - 5000); // inherited the source's idle age
    expect(rec.openedAt).toBe(NOW - 9000);
  });
});

// --- close_tab: the edge re-check is the whole point ------------------------
describe("close_tab edge re-check (§6)", () => {
  function chromeWithTab(over = {}) {
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 7, windowId: 1, url: "https://x/", audible: false, pinned: false, active: false, ...over }],
      windows: [{ id: 1, type: "normal", state: "normal" }],
      lastFocused: { id: 2, focused: true }, // a DIFFERENT window is focused by default
    });
  }

  it("precondition_failed when the tab became AUDIBLE after the snapshot; remove NOT called, no curatorCause leak", async () => {
    chromeWithTab({ audible: true }); // audible now, though the snapshot saw it silent
    const remove = vi.spyOn(chrome.tabs, "remove");
    const c = ctx();
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/", notAudible: true } }),
      c,
    );
    expect(res).toEqual({ ok: false, error: { code: "precondition_failed", message: expect.any(String) } });
    expect(remove).not.toHaveBeenCalled(); // the tab was NOT closed
    expect(c.map.markCuratorCause).not.toHaveBeenCalled(); // no cause mark leaked
    expect(c.map.clearCuratorCause).not.toHaveBeenCalled();
  });

  it("happy path: markCuratorCause is AWAITED BEFORE tabs.remove (assert order)", async () => {
    chromeWithTab();
    const order = [];
    const map = spyMap();
    map.markCuratorCause = vi.fn(async () => {
      order.push("mark");
    });
    vi.spyOn(chrome.tabs, "remove").mockImplementation(async () => {
      order.push("remove");
    });
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/", notAudible: true, notPinned: true } }),
      ctx({ map }),
    );
    expect(res).toEqual({ ok: true, result: { ok: true } });
    expect(map.markCuratorCause).toHaveBeenCalledWith(1, NOW); // the tab's window
    expect(order).toEqual(["mark", "remove"]); // cause written BEFORE the close
  });

  it("precondition_failed when pinned after the snapshot", async () => {
    chromeWithTab({ pinned: true });
    const remove = vi.spyOn(chrome.tabs, "remove");
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/", notPinned: true } }),
      ctx(),
    );
    expect(res.error.code).toBe("precondition_failed");
    expect(remove).not.toHaveBeenCalled();
  });

  it("precondition_failed when active in the focused window", async () => {
    chromeWithTab({ active: true });
    globalThis.chrome.__state.lastFocused = { id: 1, focused: true }; // the tab's window IS focused
    const remove = vi.spyOn(chrome.tabs, "remove");
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/" } }),
      ctx(),
    );
    expect(res.error.code).toBe("precondition_failed");
    expect(remove).not.toHaveBeenCalled();
  });

  it("precondition_failed when idle age fell below minIdleMs", async () => {
    chromeWithTab();
    const remove = vi.spyOn(chrome.tabs, "remove");
    // The tab was re-viewed 1s ago per the OWN map, but the command wants >=60s idle.
    const map = spyMap({ tabs: { 7: { lastActive: NOW - 1000 } } });
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/", minIdleMs: 60000 } }),
      ctx({ map }),
    );
    expect(res.error.code).toBe("precondition_failed");
    expect(remove).not.toHaveBeenCalled();
    expect(map.markCuratorCause).not.toHaveBeenCalled();
  });

  it("passes minIdleMs when the map shows the tab idle long enough", async () => {
    chromeWithTab();
    const map = spyMap({ tabs: { 7: { lastActive: NOW - 5 * 60000 } } });
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/", minIdleMs: 60000 } }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
  });

  it("precondition_failed when the url diverged from expect.url", async () => {
    chromeWithTab({ url: "https://x/moved" });
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/" } }),
      ctx(),
    );
    expect(res.error.code).toBe("precondition_failed");
  });

  it("no_such_tab when the tab is gone", async () => {
    chromeWithTab();
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 999, expect: {} }),
      ctx(),
    );
    expect(res.error.code).toBe("no_such_tab");
  });

  it("clears the curatorCause mark when the remove fails", async () => {
    chromeWithTab();
    vi.spyOn(chrome.tabs, "remove").mockRejectedValue(new Error("gone mid-flight"));
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { tabId: 7, expect: { url: "https://x/" } }),
      ctx({ map }),
    );
    expect(res.ok).toBe(false);
    expect(map.markCuratorCause).toHaveBeenCalledWith(1, NOW);
    expect(map.clearCuratorCause).toHaveBeenCalledWith(1); // undone
  });
});

// --- get_tab / focus_tab / navigate_tab -------------------------------------
describe("get / focus / navigate", () => {
  function chromeWithTab() {
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 5, windowId: 3, url: "https://y/", active: false }],
      windows: [{ id: 3, type: "normal", state: "normal" }],
    });
  }

  it("get_tab happy => {tab}", async () => {
    chromeWithTab();
    const res = await dispatchCommand(frame(CMD_GET_TAB, { tabId: 5 }), ctx());
    expect(res.ok).toBe(true);
    expect(res.result.tab).toMatchObject({ id: 5, windowId: 3 });
  });

  it("get_tab no_such_tab", async () => {
    chromeWithTab();
    const res = await dispatchCommand(frame(CMD_GET_TAB, { tabId: 42 }), ctx());
    expect(res.error.code).toBe("no_such_tab");
  });

  it("focus_tab activates the tab and focuses its window", async () => {
    chromeWithTab();
    const update = vi.spyOn(chrome.tabs, "update");
    const winUpdate = vi.spyOn(chrome.windows, "update");
    const res = await dispatchCommand(frame(CMD_FOCUS_TAB, { tabId: 5 }), ctx());
    expect(res.ok).toBe(true);
    expect(update).toHaveBeenCalledWith(5, { active: true });
    expect(winUpdate).toHaveBeenCalledWith(3, { focused: true });
  });

  it("focus_tab no_such_tab", async () => {
    chromeWithTab();
    const res = await dispatchCommand(frame(CMD_FOCUS_TAB, { tabId: 42 }), ctx());
    expect(res.error.code).toBe("no_such_tab");
  });

  it("navigate_tab happy (http/https validated) updates the url", async () => {
    chromeWithTab();
    const update = vi.spyOn(chrome.tabs, "update");
    const res = await dispatchCommand(frame(CMD_NAVIGATE_TAB, { tabId: 5, url: "https://z/" }), ctx());
    expect(res.ok).toBe(true);
    expect(update).toHaveBeenCalledWith(5, { url: "https://z/" });
  });

  it("navigate_tab rejects a non-http(s) url at the edge, tabs.update NOT called", async () => {
    chromeWithTab();
    const update = vi.spyOn(chrome.tabs, "update");
    const res = await dispatchCommand(frame(CMD_NAVIGATE_TAB, { tabId: 5, url: "javascript:1" }), ctx());
    expect(res.error.code).toBe("precondition_failed");
    expect(update).not.toHaveBeenCalled();
  });

  it("navigate_tab no_such_tab for a missing tab", async () => {
    chromeWithTab();
    const res = await dispatchCommand(frame(CMD_NAVIGATE_TAB, { tabId: 42, url: "https://z/" }), ctx());
    expect(res.error.code).toBe("no_such_tab");
  });
});

// --- merge_windows ----------------------------------------------------------
describe("merge_windows", () => {
  function chromeTwoWindows() {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://b/" },
        { id: 201, windowId: 2, url: "https://c/" },
      ],
      windows: [{ id: 1, type: "normal" }, { id: 2, type: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
  }

  it("marks BOTH source and target windows before moving; returns merged count", async () => {
    chromeTwoWindows();
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2], targetWindowId: 1 }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ merged: 2 }); // both window-2 tabs moved
    // Marked the union {source 2, target 1} before the move.
    const marked = map.markCuratorCause.mock.calls[0][0];
    expect([...marked].sort()).toEqual([1, 2]);
    expect(move).toHaveBeenCalled();
  });

  it("empty params fold only NORMAL windows into the focused normal one (§9)", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://b/" },
        { id: 300, windowId: 3, url: "https://p/" }, // lives in a POPUP window
      ],
      windows: [{ id: 1, type: "normal" }, { id: 2, type: "normal" }, { id: 3, type: "popup" }],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(frame(CMD_MERGE_WINDOWS, {}), ctx({ map }));
    expect(res.ok).toBe(true);
    // Only the normal window 2 folds into window 1; the popup (3) is untouched.
    expect(res.result).toEqual({ merged: 1 });
    expect(move.mock.calls[0][0]).toEqual([200]); // not 300 (popup)
  });

  it("busy_dragging when a drag is in progress; curatorCause cleared", async () => {
    chromeTwoWindows();
    chrome.__state.moveError = "Tabs cannot be edited right now (user may be dragging a tab).";
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2], targetWindowId: 1 }),
      ctx({ map }),
    );
    expect(res.error.code).toBe("busy_dragging");
    expect(map.markCuratorCause).toHaveBeenCalled();
    expect(map.clearCuratorCause).toHaveBeenCalled(); // rolled back
  });
});

// --- execute_js: the checkbox gate ------------------------------------------
describe("execute_js checkbox gate (§12)", () => {
  it("checkbox OFF (default) => js_disabled and executeScript NOT called", async () => {
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const res = await dispatchCommand(frame(CMD_EXECUTE_JS, { code: "1+1", tabId: 5 }), ctx());
    expect(res).toEqual({ ok: false, error: { code: "js_disabled", message: expect.any(String) } });
    expect(exec).not.toHaveBeenCalled(); // never executed
  });

  it("checkbox ON => runs executeScript in the requested world and returns {results}", async () => {
    globalThis.chrome = createChromeMock({ tabs: [{ id: 5, windowId: 1, url: "https://x/" }] });
    await chrome.storage.local.set({ allowExecuteJs: true });
    chrome.__state.scriptResults = [{ result: 4 }];
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const res = await dispatchCommand(
      frame(CMD_EXECUTE_JS, { code: "2+2", tabId: 5, world: "MAIN" }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ results: [{ result: 4 }] });
    expect(exec).toHaveBeenCalledOnce();
    const injection = exec.mock.calls[0][0];
    expect(injection.target).toEqual({ tabId: 5 });
    expect(injection.world).toBe("MAIN");
    expect(injection.args).toEqual(["2+2"]); // the curator code is passed as an arg
  });

  it("checkbox ON but target is a non-http tab => precondition_failed, NOT executed (§12)", async () => {
    // Even with <all_urls> granted, execute_js must edge-guard the target's scheme
    // so it can never inject into file:///view-source: (a MAIN-world eval on file://
    // reads local files same-origin). Drop the target guard and this reddens.
    globalThis.chrome = createChromeMock({ tabs: [{ id: 7, windowId: 1, url: "file:///etc/passwd" }] });
    await chrome.storage.local.set({ allowExecuteJs: true });
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const res = await dispatchCommand(frame(CMD_EXECUTE_JS, { code: "1", tabId: 7 }), ctx());
    expect(res).toEqual({
      ok: false,
      error: { code: "precondition_failed", message: expect.any(String) },
    });
    expect(exec).not.toHaveBeenCalled();
  });
});
