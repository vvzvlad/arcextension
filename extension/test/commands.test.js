import { describe, it, expect, beforeEach, vi } from "vitest";
import { createChromeMock } from "./chrome-mock.js";
import {
  dispatchCommand,
  evalInWorld,
  readTextInWorld,
  matchInWorld,
} from "../src/commands.js";
import * as activityMap from "../src/activity-map.js";
import {
  CMD_OPEN_TAB,
  CMD_CLOSE_TAB,
  CMD_GET_TAB,
  CMD_FOCUS_TAB,
  CMD_FOCUS_WINDOW,
  CMD_NAVIGATE_TAB,
  CMD_MERGE_WINDOWS,
  CMD_EXECUTE_JS,
  CMD_MOVE_TAB,
  CMD_GET_TEXT,
  CMD_WAIT_FOR,
  WAIT_POLL_MS,
} from "../src/constants.js";

const NOW = 1_000_000_000;
const SID = "session-1";

// A fully-spied activity map so ordering / "not called" / "no leak" assertions
// are exact. readMap is configurable per test (default: an empty map).
function spyMap(readValue = { tabs: {} }) {
  return {
    seedCuratorTab: vi.fn(async () => {}),
    seedCuratorTabs: vi.fn(async () => {}),
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
    [CMD_FOCUS_WINDOW, { windowId: 1 }],
    [CMD_NAVIGATE_TAB, { tabId: 1, url: "https://x/" }],
    [CMD_MERGE_WINDOWS, { windowIds: [2], targetWindowId: 1 }],
    [CMD_MOVE_TAB, { tabId: 1, windowId: 2 }],
    [CMD_EXECUTE_JS, { code: "1", tabId: 1 }],
    // The FIXED-function verbs skip the execute_js checkbox, NOT the session check: a
    // frame from a dead session names tab ids this extension no longer owns, so reading
    // one is reading a stranger's tab.
    [CMD_GET_TEXT, { tabId: 1 }],
    [CMD_WAIT_FOR, { tabId: 1, urlMatches: "x", timeoutMs: 1000 }],
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
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 1, url: "https://a/" }],
      windows: [{ id: 1, type: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
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

  // --- §9: the window choice is DETERMINISTIC, never "wherever create lands" ----
  it("targets the NORMAL window with the most tabs, ignoring a focused POPUP (§9)", async () => {
    // The popup is the last-focused window and has the most tabs of all; a bare
    // tabs.create would land there and the copy would be invisible to the pass
    // (phase B never completes). Drop the explicit windowId and this reddens.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 1, windowId: 1, url: "https://a/" },
        { id: 2, windowId: 2, url: "https://b/" },
        { id: 3, windowId: 2, url: "https://c/" },
        { id: 4, windowId: 3, url: "https://p1/" },
        { id: 5, windowId: 3, url: "https://p2/" },
        { id: 6, windowId: 3, url: "https://p3/" },
      ],
      windows: [
        { id: 1, type: "normal" },
        { id: 2, type: "normal" }, // 2 tabs — the biggest NORMAL window
        { id: 3, type: "popup" }, // 3 tabs, and focused — must be ignored
      ],
      lastFocused: { id: 3, type: "popup", focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());
    expect(res.ok).toBe(true);
    expect(create.mock.calls[0][0].windowId).toBe(2);
    expect(res.result.windowId).toBe(2);
  });

  it("breaks a tie on tab count by the SMALLEST windowId (§9, deterministic)", async () => {
    // Equal-sized windows listed biggest-id-first: getAll() promises no order, so the
    // tie-break must be explicit or the choice flips between calls.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 1, windowId: 9, url: "https://a/" },
        { id: 2, windowId: 4, url: "https://b/" },
      ],
      windows: [
        { id: 9, type: "normal" },
        { id: 4, type: "normal" },
      ],
      lastFocused: { id: 9, focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());
    expect(res.ok).toBe(true);
    expect(create.mock.calls[0][0].windowId).toBe(4); // smaller id wins the tie
  });

  it("never targets a FULLSCREEN window even when it has the most tabs (§9)", async () => {
    // The macOS wall dashboard: a fullscreen Grafana showcase on its own Space (§1,
    // ledger 43). §9's predicate — the service's own `_window_mergeable` — bars it from
    // BOTH roles, so a curator copy must not land in it however many tabs it holds.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 1, windowId: 1, url: "https://a/" },
        { id: 2, windowId: 7, url: "https://dash1/" },
        { id: 3, windowId: 7, url: "https://dash2/" },
        { id: 4, windowId: 7, url: "https://dash3/" },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 7, type: "normal", state: "fullscreen" }, // the showcase, and the biggest
      ],
      lastFocused: { id: 7, type: "normal", state: "fullscreen", focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());
    expect(res.ok).toBe(true);
    expect(create.mock.calls[0][0].windowId).toBe(1); // never 7
  });

  it("with ONLY a fullscreen normal window, creates a background window instead", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 2, windowId: 7, url: "https://dash/" }],
      windows: [{ id: 7, type: "normal", state: "fullscreen" }],
      lastFocused: { id: 7, type: "normal", state: "fullscreen", focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    const winCreate = vi.spyOn(chrome.windows, "create");
    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());
    expect(res.ok).toBe(true);
    expect(create).not.toHaveBeenCalled();
    expect(winCreate).toHaveBeenCalledOnce();
  });

  it("retries in a NEW window when the chosen window vanished mid-command (§9)", async () => {
    // The explicit windowId introduced a race a bare tabs.create did not have: the human
    // closes the window between getAll() and create(). Answering `internal` would send
    // the relocation to `deferred` and on to quarantine — exactly what §9 chose this
    // path to avoid. Remove the retry and this reddens with ok:false.
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 1, url: "https://a/" }],
      windows: [{ id: 1, type: "normal", state: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    create.mockRejectedValueOnce(new Error("No window with id: 1"));
    const winCreate = vi.spyOn(chrome.windows, "create");

    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());

    expect(res.ok).toBe(true);
    expect(create).toHaveBeenCalledOnce(); // the failed attempt
    expect(winCreate).toHaveBeenCalledOnce(); // then a window of our own
    expect(res.result.tabId).toBeDefined();
  });

  it("a mid-DRAG refusal is busy_dragging, NOT a new window (§9)", async () => {
    // The retry must not treat every rejection as "the window vanished": a human
    // dragging a tab makes Chromium refuse tab edits, and a pass with three relocations
    // would leave three stray background windows with no self-healing (a one-tab window
    // is never picked again). Phase A defers an open failure without a strike
    // (src/curator/phases.py), so refusing honestly is the cheaper answer.
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 1, url: "https://a/" }],
      windows: [{ id: 1, type: "normal", state: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    create.mockRejectedValueOnce(
      new Error("Tabs cannot be edited right now (user may be dragging a tab)."),
    );
    const winCreate = vi.spyOn(chrome.windows, "create");

    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());

    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("busy_dragging");
    expect(winCreate).not.toHaveBeenCalled(); // no stray window
  });

  it("an unrelated tabs.create fault stays `internal`, it does not spawn a window", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 1, url: "https://a/" }],
      windows: [{ id: 1, type: "normal", state: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
    vi.spyOn(chrome.tabs, "create").mockRejectedValueOnce(new Error("quota exceeded"));
    const winCreate = vi.spyOn(chrome.windows, "create");

    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());

    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("internal");
    expect(winCreate).not.toHaveBeenCalled();
  });

  it("with ZERO normal windows creates a BACKGROUND normal window (§9), never fails", async () => {
    // macOS: the browser lives with no windows daily. Failing here would push the
    // relocation to `deferred` and on to quarantine. Only a popup exists.
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 4, windowId: 3, url: "https://p/" }],
      windows: [{ id: 3, type: "popup" }],
      lastFocused: { id: 3, type: "popup", focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    const winCreate = vi.spyOn(chrome.windows, "create");
    const res = await dispatchCommand(
      frame(CMD_OPEN_TAB, { url: "https://x/", pinned: true }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(create).not.toHaveBeenCalled(); // no tabs.create into the popup
    expect(winCreate).toHaveBeenCalledOnce();
    const props = winCreate.mock.calls[0][0];
    expect(props).toMatchObject({ url: "https://x/", focused: false, state: "normal" });
    // The tab really exists in the new window, and `pinned` survived (windows.create
    // takes no pinned flag, so it is applied to the created tab afterwards).
    const tabs = await chrome.tabs.query({});
    const opened = tabs.find((t) => t.url === "https://x/");
    expect(opened.windowId).toBe(res.result.windowId);
    expect(opened.pinned).toBe(true);
  });

  // --- #45: window as address -----------------------------------------------
  it("#45: opens in a NAMED windowId, overriding the §9 auto-pick", async () => {
    // Window 1 has the MOST tabs, so the auto-select would land there; naming window 2
    // must win. Drop the explicit-window branch and this reddens (create lands in 1).
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 1, windowId: 1, url: "https://a/" },
        { id: 3, windowId: 1, url: "https://a2/" },
        { id: 2, windowId: 2, url: "https://b/" },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" }, // 2 tabs — the auto-pick
        { id: 2, type: "normal", state: "normal" }, // 1 tab — the NAMED window
      ],
      lastFocused: { id: 1, focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    const res = await dispatchCommand(
      frame(CMD_OPEN_TAB, { url: "https://x/", windowId: 2 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(create.mock.calls[0][0].windowId).toBe(2); // the caller's window, not §9's 1
    expect(res.result.windowId).toBe(2);
  });

  it("#45: a NAMED fullscreen (or popup) windowId is refused with no_window, nothing created", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 1, url: "https://a/" }],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 7, type: "normal", state: "fullscreen" }, // the wall-dashboard showcase
        { id: 8, type: "popup", state: "normal" },
      ],
      lastFocused: { id: 1, focused: true },
    });
    for (const badId of [7, 8]) {
      const create = vi.spyOn(chrome.tabs, "create");
      const winCreate = vi.spyOn(chrome.windows, "create");
      const res = await dispatchCommand(
        frame(CMD_OPEN_TAB, { url: "https://x/", windowId: badId }),
        ctx(),
      );
      expect(res.ok, `windowId ${badId}`).toBe(false);
      expect(res.error.code).toBe("no_window");
      expect(create).not.toHaveBeenCalled(); // no tab created into the ineligible window
      expect(winCreate).not.toHaveBeenCalled(); // and NO fallback window either
      create.mockRestore();
      winCreate.mockRestore();
    }
  });

  it("#45: a NAMED window that vanished before create is no_window, never a fallback window", async () => {
    // The window is present at getAll() (passes validation) but gone by create() — the
    // human closed it in the gap. Unlike the AUTO-SELECT path, a caller-named window is
    // NOT retried in one of our own: silently relocating would put the tab where the
    // caller did not ask, and an OLD extension ignoring the key is what the server
    // cross-check catches. Turn the refusal into a retry and this reddens.
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 2, url: "https://a/" }],
      windows: [{ id: 2, type: "normal", state: "normal" }],
      lastFocused: { id: 2, focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    create.mockRejectedValueOnce(new Error("No window with id: 2"));
    const winCreate = vi.spyOn(chrome.windows, "create");
    const res = await dispatchCommand(
      frame(CMD_OPEN_TAB, { url: "https://x/", windowId: 2 }),
      ctx(),
    );
    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("no_window");
    expect(winCreate).not.toHaveBeenCalled(); // no window of our own
  });

  it("#45: WITHOUT a windowId, the vanished-window retry still fires (auto-select unchanged)", async () => {
    // Acceptance 5 / compatibility: the curator's own pass names no window, so its
    // auto-select AND its self-healing retry must be exactly as before. This is the
    // mirror of the test above: same race, opposite handling, because WE chose the window.
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 1, url: "https://a/" }],
      windows: [{ id: 1, type: "normal", state: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
    const create = vi.spyOn(chrome.tabs, "create");
    create.mockRejectedValueOnce(new Error("No window with id: 1"));
    const winCreate = vi.spyOn(chrome.windows, "create");
    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { url: "https://x/" }), ctx());
    expect(res.ok).toBe(true); // retried into a window of our own
    expect(winCreate).toHaveBeenCalledOnce();
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

  it("focus_window raises the window WITHOUT touching any tab", async () => {
    chromeWithTab();
    const tabUpdate = vi.spyOn(chrome.tabs, "update");
    const winUpdate = vi.spyOn(chrome.windows, "update");
    const res = await dispatchCommand(frame(CMD_FOCUS_WINDOW, { windowId: 3 }), ctx());
    expect(res.ok).toBe(true);
    expect(winUpdate).toHaveBeenCalledWith(3, { focused: true });
    // The whole point of focus_window: no tab is activated (contrast focus_tab).
    expect(tabUpdate).not.toHaveBeenCalled();
  });

  it("focus_window maps a missing window to no_window and updates nothing", async () => {
    chromeWithTab();
    const winUpdate = vi.spyOn(chrome.windows, "update");
    const res = await dispatchCommand(frame(CMD_FOCUS_WINDOW, { windowId: 999 }), ctx());
    expect(res.error.code).toBe("no_window");
    expect(winUpdate).not.toHaveBeenCalled();
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

  it("empty params fold only NORMAL windows into the FOCUSED normal one (§9)", async () => {
    // Criterion 1 of the manual "{}" button: the focused normal window is the TARGET
    // even though window 2 has more tabs. That is deliberate — the edge re-check
    // refuses to move a window whose active tab is on screen, so a focused window used
    // as a SOURCE would just be dropped and the button would leave unmerged exactly the
    // window the human is looking at. Popups are excluded from both roles.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://b/" },
        { id: 201, windowId: 2, url: "https://b2/" }, // window 2 is the LARGER one
        { id: 300, windowId: 3, url: "https://p/" }, // lives in a POPUP window
      ],
      windows: [{ id: 1, type: "normal" }, { id: 2, type: "normal" }, { id: 3, type: "popup" }],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(frame(CMD_MERGE_WINDOWS, {}), ctx({ map }));
    expect(res.ok).toBe(true);
    // The normal window 2 folds into the focused window 1; the popup (3) is untouched.
    expect(res.result).toEqual({ merged: 2 });
    expect(move.mock.calls[0][0]).toEqual([200, 201]); // not 300 (popup)
    expect(move.mock.calls[0][1].windowId).toBe(1); // the FOCUSED window is the target
  });

  it("empty params with NO focused normal window target the LARGEST normal window (§9)", async () => {
    // Criterion 2: §9 names the target "обычное окно с наибольшим числом вкладок". The
    // old fallback was `normalWindows[0]` — getAll() promises no order, so the target
    // flipped between calls AND could be the smallest window, making the merge move far
    // more tabs than necessary. Windows are listed with the largest LAST on purpose:
    // with the old fallback the target would be 5 and this reddens.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 5, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://b/" },
        { id: 300, windowId: 9, url: "https://c1/" },
        { id: 301, windowId: 9, url: "https://c2/" },
        { id: 302, windowId: 9, url: "https://c3/" },
      ],
      windows: [{ id: 5, type: "normal" }, { id: 2, type: "normal" }, { id: 9, type: "normal" }],
      lastFocused: { id: -1, focused: false }, // macOS: no window focused at all
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(frame(CMD_MERGE_WINDOWS, {}), ctx());
    expect(res.ok).toBe(true);
    expect(move.mock.calls[0][1].windowId).toBe(9); // most tabs => fewest moves
    expect(move.mock.calls[0][0].sort()).toEqual([100, 200]); // the two small windows fold in
    expect(res.result).toEqual({ merged: 2 });
  });

  it("empty params break a target tie by the SMALLEST windowId (§9, deterministic)", async () => {
    // Equal-sized normal windows listed largest-id-first: the choice must not depend on
    // getAll() order. The same rule (and the same helper) open_tab uses.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 9, url: "https://a1/" },
        { id: 101, windowId: 9, url: "https://a2/" },
        { id: 200, windowId: 4, url: "https://b1/" },
        { id: 201, windowId: 4, url: "https://b2/" },
      ],
      windows: [{ id: 9, type: "normal" }, { id: 4, type: "normal" }],
      lastFocused: { id: -1, focused: false },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(frame(CMD_MERGE_WINDOWS, {}), ctx());
    expect(res.ok).toBe(true);
    expect(move.mock.calls[0][1].windowId).toBe(4); // smaller id wins the tie
    expect(move.mock.calls[0][0]).toEqual([100, 101]);
    expect(res.result).toEqual({ merged: 2 });
  });

  it("empty params with a POPUP focused fall back to the deterministic rule, not the popup (§9)", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 5, url: "https://a/" },
        { id: 300, windowId: 9, url: "https://c1/" },
        { id: 301, windowId: 9, url: "https://c2/" },
        { id: 400, windowId: 3, url: "https://p/" },
      ],
      windows: [{ id: 5, type: "normal" }, { id: 9, type: "normal" }, { id: 3, type: "popup" }],
      lastFocused: { id: 3, type: "popup", focused: true }, // a popup is on top
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(frame(CMD_MERGE_WINDOWS, {}), ctx());
    expect(res.ok).toBe(true);
    expect(move.mock.calls[0][1].windowId).toBe(9); // never the popup
    expect(move.mock.calls[0][0]).toEqual([100]); // popup tab 400 stays put
  });

  it("moves ONLY unpinned tabs; pinned tabs stay, the window is not emptied (§9)", async () => {
    // A cross-window tabs.move resets `pinned` (§9 trap) — pinned tabs must never
    // migrate. Window 2 holds one pinned + one unpinned tab: only the unpinned one
    // moves, so window 2 keeps its pinned tab and does not vanish. Drop `!t.pinned`
    // from the toMove filter and this reddens (the pinned tab 200 would move too).
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/", pinned: false },
        { id: 200, windowId: 2, url: "https://pin/", pinned: true },
        { id: 201, windowId: 2, url: "https://b/", pinned: false },
      ],
      windows: [{ id: 1, type: "normal" }, { id: 2, type: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2], targetWindowId: 1 }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ merged: 1 }); // only the unpinned tab 201 moved
    expect(move.mock.calls[0][0]).toEqual([201]); // NOT 200 (pinned)
    // BOTH windows were marked BEFORE the move (§6: no rejuvenation of either side).
    const marked = map.markCuratorCause.mock.calls[0][0];
    expect([...marked].sort()).toEqual([1, 2]);
    // The pinned tab is still in window 2 (never moved) => the window survives.
    const stillThere = (await chrome.tabs.query({})).find((t) => t.id === 200);
    expect(stillThere.windowId).toBe(2);
  });

  it("a source window of ONLY pinned tabs yields an empty move (§9)", async () => {
    // Window 2 is all-pinned: nothing may migrate, so the move set is empty and the
    // window stays. (chrome.tabs.move must not be called with an empty list.)
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/", pinned: false },
        { id: 200, windowId: 2, url: "https://pin1/", pinned: true },
        { id: 201, windowId: 2, url: "https://pin2/", pinned: true },
      ],
      windows: [{ id: 1, type: "normal" }, { id: 2, type: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2], targetWindowId: 1 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ merged: 0 });
    expect(move).not.toHaveBeenCalled(); // no empty tabs.move
  });

  it("EDGE re-check: a source the owner returned to (active tab in the focused window) is NOT moved (§9)", async () => {
    // The merge was decided on the step-3 snapshot; if the owner focused window 2 and
    // its active tab is on screen in the sub-second gap before step 9, that window must
    // NOT collapse (parity with close_tab's expect). Drop the edge re-check and this
    // reddens (tab 200 would move into window 1).
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://b/", active: true },
      ],
      windows: [{ id: 1, type: "normal" }, { id: 2, type: "normal" }],
      lastFocused: { id: 2, type: "normal", focused: true }, // owner is IN window 2 now
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2], targetWindowId: 1 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ merged: 0 }); // window 2 dropped by the edge re-check
    expect(move).not.toHaveBeenCalled();
  });

  it("EDGE re-check: a source with an audible tab is NOT moved; an unfocused active source still moves (§9)", async () => {
    // Audible => dropped even if unfocused (background media). Control: window 3's
    // active tab is NOT in the focused window (1), so it is NOT on screen and DOES move
    // — proving the re-check is specific, not a blanket refusal.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://sound/", audible: true },
        { id: 300, windowId: 3, url: "https://c/", active: true }, // active but window 3 not focused
      ],
      windows: [
        { id: 1, type: "normal" }, { id: 2, type: "normal" }, { id: 3, type: "normal" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2, 3], targetWindowId: 1 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ merged: 1 }); // only window 3 (not the audible window 2)
    expect(move.mock.calls[0][0]).toEqual([300]);
  });

  it("a FULLSCREEN focused window is NEVER the merge target (§9)", async () => {
    // THE blocker: `{}` (the manual button) with a fullscreen showcase focused. §9 and
    // the service's `_window_mergeable` say a fullscreen window is "neither folded nor
    // merged into", and a window merge is NOT undoable — dumping every other window's
    // tabs into the wall dashboard is unrecoverable. The target must fall back to the
    // deterministic rule among the MERGEABLE windows.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://b1/" },
        { id: 201, windowId: 2, url: "https://b2/" },
        { id: 300, windowId: 7, url: "https://dash/" },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 2, type: "normal", state: "normal" },
        { id: 7, type: "normal", state: "fullscreen" },
      ],
      lastFocused: { id: 7, type: "normal", state: "fullscreen", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(frame(CMD_MERGE_WINDOWS, {}), ctx());
    expect(res.ok).toBe(true);
    // Target = window 2 (most tabs among mergeable), NOT the focused fullscreen 7.
    expect(move.mock.calls[0][1].windowId).toBe(2);
    expect(move.mock.calls[0][0]).toEqual([100]); // only window 1 folds
    // The showcase keeps its tab and gains nothing.
    const dash = (await chrome.tabs.query({})).find((t) => t.id === 300);
    expect(dash.windowId).toBe(7);
  });

  it("an EXPLICIT fullscreen targetWindowId is refused with no_window (§9)", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 300, windowId: 7, url: "https://dash/" },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 7, type: "normal", state: "fullscreen" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [1], targetWindowId: 7 }),
      ctx(),
    );
    expect(res).toEqual({ ok: false, error: { code: "no_window", message: expect.any(String) } });
    expect(move).not.toHaveBeenCalled();
  });

  it("a target window that CLOSED since the snapshot answers no_window, not internal", async () => {
    // The service decided on the step-3 snapshot; a pass runs for minutes. `no_window`
    // is in the service's _CLIENT_ERRORS (src/api/instances.py) => 409 + refetch, so the
    // page re-reads state. `internal` would become a 502 and nothing would re-read.
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 100, windowId: 1, url: "https://a/" }],
      windows: [{ id: 1, type: "normal", state: "normal" }],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [1], targetWindowId: 42 }), // 42 is gone
      ctx(),
    );
    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("no_window");
    expect(move).not.toHaveBeenCalled();
  });

  it("an EXPLICIT popup targetWindowId is refused too (§9)", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 300, windowId: 3, url: "https://p/" },
      ],
      windows: [
        { id: 1, type: "normal" },
        { id: 3, type: "popup" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [1], targetWindowId: 3 }),
      ctx(),
    );
    expect(res.error.code).toBe("no_window");
  });

  it("EDGE re-check: a FULLSCREEN source window is NOT merged (§9)", async () => {
    // §9's guard is literally "обычное окно в состоянии не fullscreen". A window the
    // owner just put fullscreen (a video, a presentation) is in use even with a silent,
    // inactive tab — the audible/on-screen pair does not catch it. Control: window 3 is
    // the same shape but `normal`, and it DOES move.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://full/" },
        { id: 300, windowId: 3, url: "https://c/" },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 2, type: "normal", state: "fullscreen" },
        { id: 3, type: "normal", state: "normal" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2, 3], targetWindowId: 1 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ merged: 1 }); // only window 3
    expect(move.mock.calls[0][0]).toEqual([300]); // NOT 200 (fullscreen)
  });

  it("filters EXPLICIT windowIds by type === normal too, not just the {} branch (§9)", async () => {
    // The service names its sources from the step-3 snapshot; by command time one may
    // be a popup (or the mirror is simply stale). Folding a popup's tabs is the exact
    // failure the manual branch already guards. Drop the filter and 300 moves too.
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/" },
        { id: 200, windowId: 2, url: "https://b/" },
        { id: 300, windowId: 3, url: "https://p/" }, // lives in a POPUP window
      ],
      windows: [
        { id: 1, type: "normal" },
        { id: 2, type: "normal" },
        { id: 3, type: "popup" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MERGE_WINDOWS, { windowIds: [2, 3], targetWindowId: 1 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ merged: 1 });
    expect(move.mock.calls[0][0]).toEqual([200]); // NOT 300 (popup)
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

// --- move_tab ---------------------------------------------------------------
describe("move_tab", () => {
  // Two normal windows; tab 100 lives in window 1, window 2 already holds two tabs.
  function chromeTwoNormalWindows(over = {}) {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/", pinned: false },
        { id: 200, windowId: 2, url: "https://b/", pinned: false },
        { id: 201, windowId: 2, url: "https://c/", pinned: false },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 2, type: "normal", state: "normal" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
      ...over,
    });
  }

  function tabById(id) {
    return chrome.__state.tabs.find((t) => t.id === id);
  }

  it("moves a tab to ANOTHER window and marks BOTH windows as curator-caused", async () => {
    // The whole point of the verb: inside one browser there was no way to relocate a
    // tab (cross-instance relocation is open+close, which needs two processes).
    chromeTwoNormalWindows();
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ tabId: 100, windowId: 2, index: -1 });
    expect(move).toHaveBeenCalledWith(100, { windowId: 2, index: -1 });
    expect(tabById(100).windowId).toBe(2); // it really moved

    // Both the source and the target are reshuffled by the move, so both are marked
    // BEFORE it — without this the agent's move reads as "the human touched this tab"
    // and resets the very idle clock the tab was moved by (§5/§6).
    expect(map.markCuratorCause).toHaveBeenCalledTimes(1);
    const [marked, when] = map.markCuratorCause.mock.calls[0];
    expect([...marked].sort()).toEqual([1, 2]);
    expect(when).toBe(NOW);
    expect(map.clearCuratorCause).not.toHaveBeenCalled(); // nothing to roll back
  });

  it("moves a tab WITHIN its own window; only that window is marked", async () => {
    chromeTwoNormalWindows();
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 200, windowId: 2, index: 0 }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ tabId: 200, windowId: 2, index: 0 });
    expect(move).toHaveBeenCalledWith(200, { windowId: 2, index: 0 });
    expect(map.markCuratorCause.mock.calls[0][0]).toEqual([2]); // deduped to one window
  });

  it("index defaults to -1 (the end) and an EXPLICIT index is passed through", async () => {
    chromeTwoNormalWindows();
    const move = vi.spyOn(chrome.tabs, "move");
    await dispatchCommand(frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }), ctx());
    expect(move.mock.calls[0][1]).toEqual({ windowId: 2, index: -1 });

    chromeTwoNormalWindows();
    const move2 = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2, index: 1 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(move2.mock.calls[0][1]).toEqual({ windowId: 2, index: 1 });
  });

  it("a PINNED tab across windows is REFUSED: nothing moves, it stays pinned (§9)", async () => {
    // §9's rule, verbatim: a cross-window tabs.move silently drops `pinned`, and the
    // lost turn between the move and re-pinning destroys the owner's only "do not
    // touch by hand" shield. merge_windows can skip such a tab silently because it
    // moves a SET and the skip shows up in `merged`; a one-tab verb cannot — answering
    // ok while doing nothing would tell the agent the tab moved. Hence a code of its
    // own, and no side effects at all.
    chromeTwoNormalWindows();
    chrome.__state.tabs.find((t) => t.id === 100).pinned = true;
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }),
      ctx({ map }),
    );
    expect(res).toEqual({
      ok: false,
      error: { code: "pinned_cross_window", message: expect.any(String) },
    });
    expect(move).not.toHaveBeenCalled();
    expect(map.markCuratorCause).not.toHaveBeenCalled(); // no mark to roll back either
    // The tab is exactly where it was, and still shielded.
    expect(tabById(100).windowId).toBe(1);
    expect(tabById(100).pinned).toBe(true);
  });

  it("a PINNED tab moves freely INSIDE its window and keeps `pinned` (§9)", async () => {
    // The other half of the same rule: an intra-window move preserves `pinned`, so
    // there is no shield to lose and nothing to refuse. Widen the refusal to every
    // move and this reddens.
    chromeTwoNormalWindows();
    chrome.__state.tabs.find((t) => t.id === 200).pinned = true;
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 200, windowId: 2, index: 0 }),
      ctx(),
    );
    expect(res.ok).toBe(true);
    expect(move).toHaveBeenCalledWith(200, { windowId: 2, index: 0 });
    expect(tabById(200).pinned).toBe(true);
    expect(tabById(200).windowId).toBe(2);
  });

  it("an INELIGIBLE target window (popup / devtools) is refused with no_window (§9)", async () => {
    // The SAME predicate merge_windows uses for its target: there is no reason to let
    // an agent dump tabs into a devtools or popup window.
    chromeTwoNormalWindows({
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 2, type: "popup", state: "normal" },
      ],
    });
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }), ctx());
    expect(res.error.code).toBe("no_window");
    expect(move).not.toHaveBeenCalled();
    expect(tabById(100).windowId).toBe(1);
  });

  it("a FULLSCREEN target window is refused too (§9's showcase)", async () => {
    chromeTwoNormalWindows({
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 2, type: "normal", state: "fullscreen" },
      ],
    });
    const res = await dispatchCommand(frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }), ctx());
    expect(res.error.code).toBe("no_window");
  });

  it("a target window that CLOSED since the agent looked answers no_window, not internal", async () => {
    // `no_window` is in the service's _CLIENT_ERRORS set => "your picture is stale,
    // refetch", where `internal` would be a 502 and nothing would re-read.
    chromeTwoNormalWindows();
    const res = await dispatchCommand(frame(CMD_MOVE_TAB, { tabId: 100, windowId: 42 }), ctx());
    expect(res.error.code).toBe("no_window");
  });

  it("a VANISHED tab answers no_such_tab, and never touches the activity map", async () => {
    // Between the agent's decision and this command the human may simply have closed
    // the tab. That must be a clear answer, not a throw that becomes `internal`.
    chromeTwoNormalWindows();
    const move = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 999, windowId: 2 }),
      ctx({ map }),
    );
    expect(res).toEqual({
      ok: false,
      error: { code: "no_such_tab", message: expect.any(String) },
    });
    expect(move).not.toHaveBeenCalled();
    expect(map.markCuratorCause).not.toHaveBeenCalled();
  });

  it("a tab that vanishes BETWEEN the get and the move is still no_such_tab", async () => {
    // The narrower race: it existed when we looked and is gone when Chromium executes.
    // Chromium says "No tab with id: N" — that is the vanished tab, not a fault.
    chromeTwoNormalWindows();
    chrome.__state.moveError = "No tab with id: 100.";
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }),
      ctx({ map }),
    );
    expect(res.error.code).toBe("no_such_tab");
    expect(map.clearCuratorCause).toHaveBeenCalled(); // the mark is rolled back
  });

  it("busy_dragging while the human holds a tab; curatorCause cleared", async () => {
    chromeTwoNormalWindows();
    chrome.__state.moveError = "Tabs cannot be edited right now (user may be dragging a tab).";
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }),
      ctx({ map }),
    );
    expect(res.error.code).toBe("busy_dragging");
    expect(map.markCuratorCause).toHaveBeenCalled();
    expect(map.clearCuratorCause).toHaveBeenCalled();
  });

  it("any other move failure is `internal`, with the mark rolled back", async () => {
    chromeTwoNormalWindows();
    chrome.__state.moveError = "something else went wrong";
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: 2 }),
      ctx({ map }),
    );
    expect(res.error.code).toBe("internal");
    expect(map.clearCuratorCause).toHaveBeenCalled();
  });

  it("a missing/garbled windowId or index is refused at the edge, before any read", async () => {
    for (const params of [
      { tabId: 100 }, // no target window at all
      { tabId: 100, windowId: "2" }, // a string id would silently never match
      { tabId: 100, windowId: 2, index: -2 }, // below chrome's own -1 floor
      { tabId: 100, windowId: 2, index: 1.5 },
      { tabId: 100, windowId: 2, index: "0" },
    ]) {
      chromeTwoNormalWindows();
      const move = vi.spyOn(chrome.tabs, "move");
      const res = await dispatchCommand(frame(CMD_MOVE_TAB, params), ctx());
      expect(res.ok, JSON.stringify(params)).toBe(false);
      expect(res.error.code, JSON.stringify(params)).toBe("precondition_failed");
      expect(move).not.toHaveBeenCalled();
    }
  });

  // --- #45: windowId:null extracts the tab into a NEW window -----------------
  it("#45: windowId:null extracts the tab into a NEW unfocused window (both windows marked)", async () => {
    chromeTwoNormalWindows();
    const winCreate = vi.spyOn(chrome.windows, "create");
    const tabsMove = vi.spyOn(chrome.tabs, "move");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: null }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    // windows.create MOVES the tab in — no tabs.move, no new tab id — unfocused + normal.
    expect(winCreate).toHaveBeenCalledWith({ tabId: 100, focused: false, state: "normal" });
    expect(tabsMove).not.toHaveBeenCalled();
    // Same response shape as a normal move; windowId is the CREATED window (mock: 500).
    expect(res.result).toEqual({ tabId: 100, windowId: 500, index: 0 });
    expect(tabById(100).windowId).toBe(500); // it really moved into the new window
    // BOTH windows are marked: the SOURCE before the move, and the NEW window AFTER
    // create (once its id exists), so the onActivated Chrome fires in the new window is
    // suppressed and cannot rejuvenate the extracted tab.
    expect(map.markCuratorCause).toHaveBeenCalledTimes(2);
    expect(map.markCuratorCause.mock.calls[0][0]).toBe(1);   // source, before the move
    expect(map.markCuratorCause.mock.calls[1][0]).toBe(500); // new window, after create
    expect(map.clearCuratorCause).not.toHaveBeenCalled();
  });

  it("#45: extracting a PINNED tab is refused with pinned_cross_window; it stays put and pinned", async () => {
    // windows.create({tabId}) strips `pinned` down the same Chromium path a cross-window
    // tabs.move takes, so the §9 shield must cover the new-window case too.
    chromeTwoNormalWindows();
    tabById(100).pinned = true;
    const winCreate = vi.spyOn(chrome.windows, "create");
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: null }),
      ctx({ map }),
    );
    expect(res).toEqual({
      ok: false,
      error: { code: "pinned_cross_window", message: expect.any(String) },
    });
    expect(winCreate).not.toHaveBeenCalled();
    expect(map.markCuratorCause).not.toHaveBeenCalled(); // refused before any mark
    expect(tabById(100).windowId).toBe(1); // unmoved
    expect(tabById(100).pinned).toBe(true); // still shielded
  });

  it("#45: a tab that vanished between the get and windows.create is no_such_tab, mark rolled back", async () => {
    chromeTwoNormalWindows();
    chrome.__state.createWindowError = "No tab with id: 100.";
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: null }),
      ctx({ map }),
    );
    expect(res.error.code).toBe("no_such_tab");
    expect(map.markCuratorCause).toHaveBeenCalled();
    expect(map.clearCuratorCause).toHaveBeenCalled(); // source mark undone
  });

  it("#45: extraction PRESERVES the clock — seedCuratorTab restores the pre-move age", async () => {
    // Chrome fires onActivated in the new window (unmarkable — its id does not exist yet),
    // which would rejuvenate the tab. The command reads the tab's age BEFORE the move and
    // re-seeds it after, so the next pass sees it exactly as old as before (§5). Remove
    // the restore and seedCuratorTab is never called => this reddens.
    chromeTwoNormalWindows();
    const map = spyMap({
      tabs: { 100: { lastActive: NOW - 100_000, openedAt: NOW - 200_000, ageUnknown: false } },
    });
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: null }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(map.seedCuratorTab).toHaveBeenCalledTimes(1);
    const [seededTabId, seed, when] = map.seedCuratorTab.mock.calls[0];
    expect(seededTabId).toBe(100);
    expect(seed).toEqual({
      seed_age_ms: 100_000, // exactly the age it had, not 0 (freshly touched)
      seed_opened_ago_ms: 200_000,
      seed_age_unknown: false,
    });
    expect(when).toBe(NOW);
  });

  it("#45: extraction preserves the clock END-TO-END — a later onActivated in the new window does not rejuvenate", async () => {
    // Outcome test on the REAL activity map (not the spy): seed tab 100 old, extract it
    // into a new window, THEN fire the onActivated Chrome delivers in that fresh window.
    // The new window is marked curatorCause after create, so the activation is suppressed
    // and the re-seeded age stands — the next pass sees the tab exactly as old as before
    // (§5). Reddens WITHOUT the post-create markCuratorCause(newWindowId): the late
    // onActivated would then stamp lastActive = NOW. This is what the spy test above (which
    // only checks seedCuratorTab was CALLED) cannot catch.
    chromeTwoNormalWindows();
    await activityMap.onCreated(100, NOW - 100_000); // record: lastActive=openedAt old
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: null }),
      { sessionId: SID, now: () => NOW }, // no map override => real activityMap
    );
    expect(res.ok).toBe(true);
    const newWindowId = res.result.windowId;
    // Chrome now delivers onActivated for the moved tab in the fresh window.
    await activityMap.onActivated(100, newWindowId, NOW);
    const map = await activityMap.readMap();
    expect(map.tabs[100].lastActive).toBe(NOW - 100_000); // preserved, NOT rejuvenated to NOW
  });

  it("#45: extraction PRESERVES self-navigation churn — an auto-refresh tab keeps selfNavigating", async () => {
    // The extracted tab keeps its id across windows.create, so its churn (docChanges/
    // lastDocKey/selfNavigating) must be CARRIED, not reset. Otherwise a self-navigating
    // tab (an auto-refresh dashboard) loses the flag and its next doc change re-juvenates
    // it, undoing §5 clock-preservation. Reddens if seedCuratorTab resets the churn.
    chromeTwoNormalWindows();
    await activityMap.onCreated(100, NOW - 100_000);
    // Drive it self-navigating: more than SELF_NAV_LIMIT distinct document changes.
    for (let k = 0; k <= 10; k += 1) {
      await activityMap.onDocumentChange(100, `https://dash.test/${k}`, NOW);
    }
    let m = await activityMap.readMap();
    expect(m.tabs[100].selfNavigating).toBe(true); // precondition
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { tabId: 100, windowId: null }),
      { sessionId: SID, now: () => NOW }, // real activityMap
    );
    expect(res.ok).toBe(true);
    m = await activityMap.readMap();
    expect(m.tabs[100].selfNavigating).toBe(true); // carried through, not reset to false
    expect(m.tabs[100].docChanges.length).toBeGreaterThan(10); // churn ring preserved
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
    // The code plus the awaitPromise flag; false is the default eval path (unchanged).
    expect(injection.args).toEqual(["2+2", false]);
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

// --- #49 bulk verbs: ONE frame per list, looped item by item ----------------
describe("#49 bulk close_tab {items}", () => {
  function chromeTwoWindows() {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/", pinned: false, audible: false, active: false },
        { id: 200, windowId: 2, url: "https://b/", pinned: false, audible: false, active: false },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 2, type: "normal", state: "normal" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
  }

  it("acceptance 1: three items, one already closed => two ok, the gone one no_such_tab", async () => {
    chromeTwoWindows(); // tabs 100, 200 exist; 999 does not
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { items: [{ tabId: 100 }, { tabId: 999 }, { tabId: 200 }] }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(res.result.results).toEqual([
      { index: 0, ok: true, tabId: 100 },
      { index: 1, ok: false, tabId: 999, error: "no_such_tab" },
      { index: 2, ok: true, tabId: 200 },
    ]);
    // The two live tabs were really removed; the missing one changed nothing.
    expect(chrome.__state.tabs.map((t) => t.id)).toEqual([]);
  });

  it("acceptance 3: ONE markCuratorCause with ALL affected windows, not one per item", async () => {
    chromeTwoWindows();
    const map = spyMap();
    await dispatchCommand(
      frame(CMD_CLOSE_TAB, { items: [{ tabId: 100 }, { tabId: 200 }] }),
      ctx({ map }),
    );
    expect(map.markCuratorCause).toHaveBeenCalledTimes(1);
    const [windows, when] = map.markCuratorCause.mock.calls[0];
    expect(new Set(windows)).toEqual(new Set([1, 2])); // both windows in ONE call
    expect(when).toBe(NOW);
  });

  it("whole-batch failure (every live item's remove throws) rolls the marks back", async () => {
    chromeTwoWindows();
    // EXISTING tabs 100 (win 1) and 200 (win 2), but every chrome.tabs.remove REJECTS. So
    // the windows ARE affected (affected=[1,2]) yet removed stays 0 => the rollback branch
    // (removed===0 && affected.length>0) actually runs — the opposite of the previous
    // 998/999 version, where the gone ids left affected empty and the branch never fired.
    chrome.__state.removeError = "Tabs cannot be edited right now.";
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { items: [{ tabId: 100 }, { tabId: 200 }] }),
      ctx({ map }),
    );
    expect(res.result.results.every((r) => r.ok === false)).toBe(true);
    // Nothing closed (removed===0) but both windows were marked => the marks are rolled back
    // with EXACTLY those affected windows.
    expect(map.clearCuratorCause).toHaveBeenCalledTimes(1);
    expect(new Set(map.clearCuratorCause.mock.calls[0][0])).toEqual(new Set([1, 2]));
  });

  it("partial batch clears the mark of a window that saw NO successful close (§5)", async () => {
    // A bulk close of [active-in-focus tab (guard-refused), a tab in another window
    // (closed)] marks BOTH windows up front, but only window 2 saw a real close. Window 1
    // (the FOCUS window, where the user is likely acting) must have its mark CLEARED — else
    // a real onActivated there is falsely suppressed for CURATOR_CAUSE_WINDOW_MS. Reddens if
    // the unused-window mark is retained (the pre-fix behaviour).
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/", active: true },
        { id: 200, windowId: 2, url: "https://b/" },
      ],
      windows: [{ id: 1, type: "normal" }, { id: 2, type: "normal" }],
      lastFocused: { id: 1, focused: true },
    });
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, { items: [{ tabId: 100 }, { tabId: 200 }] }),
      ctx({ map }),
    );
    expect(res.result.results[0].ok).toBe(false); // active-in-focus: refused, no close in win 1
    expect(res.result.results[1].ok).toBe(true); // closed in win 2
    // Marked both up front; cleared ONLY window 1 (no successful close), kept window 2.
    expect(map.markCuratorCause).toHaveBeenCalledTimes(1);
    expect(new Set(map.markCuratorCause.mock.calls[0][0])).toEqual(new Set([1, 2]));
    expect(map.clearCuratorCause).toHaveBeenCalledTimes(1);
    expect(map.clearCuratorCause.mock.calls[0][0]).toEqual([1]); // the unused window only
  });

  it("per-item expect (the bulk-relocate close) re-checks guards live; audible one refused", async () => {
    chromeTwoWindows();
    chrome.__state.tabs.find((t) => t.id === 200).audible = true;
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_CLOSE_TAB, {
        items: [
          { tabId: 100, expect: { url: "https://a/", notAudible: true, notPinned: true } },
          { tabId: 200, expect: { url: "https://b/", notAudible: true, notPinned: true } },
        ],
      }),
      ctx({ map }),
    );
    expect(res.result.results[0]).toEqual({ index: 0, ok: true, tabId: 100 });
    expect(res.result.results[1].ok).toBe(false);
    expect(res.result.results[1].error).toBe("precondition_failed");
    expect(chrome.__state.tabs.find((t) => t.id === 200)).toBeDefined(); // the audible tab stayed
  });
});

describe("#49 bulk move_tab {items, windowId}", () => {
  function threeWindowsWithPinned() {
    globalThis.chrome = createChromeMock({
      tabs: [
        { id: 100, windowId: 1, url: "https://a/", pinned: false },
        { id: 101, windowId: 1, url: "https://p/", pinned: true }, // pinned, will cross
        { id: 300, windowId: 3, url: "https://d/", pinned: false },
      ],
      windows: [
        { id: 1, type: "normal", state: "normal" },
        { id: 3, type: "normal", state: "normal" },
      ],
      lastFocused: { id: 1, type: "normal", focused: true },
    });
  }

  it("acceptance 4: a pinned cross-window item gets pinned_cross_window and stays; others move", async () => {
    threeWindowsWithPinned();
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { items: [{ tabId: 100 }, { tabId: 101 }, { tabId: 300 }], windowId: 3 }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(res.result.results[0]).toEqual({ index: 0, ok: true, tabId: 100, windowId: 3 });
    expect(res.result.results[1]).toMatchObject({ index: 1, ok: false, tabId: 101, error: "pinned_cross_window" });
    expect(res.result.results[2].ok).toBe(true); // 300 already in target, still ok
    // The pinned tab did NOT move; the unpinned one did.
    expect(chrome.__state.tabs.find((t) => t.id === 101).windowId).toBe(1);
    expect(chrome.__state.tabs.find((t) => t.id === 100).windowId).toBe(3);
    // ONE mark covering the target + every source window.
    expect(map.markCuratorCause).toHaveBeenCalledTimes(1);
    expect(new Set(map.markCuratorCause.mock.calls[0][0])).toEqual(new Set([3, 1]));
  });

  it("whole-batch failure (every move throws) rolls the marks back", async () => {
    threeWindowsWithPinned();
    // Every chrome.tabs.move REJECTS, so moved stays 0 while the target + source windows were
    // marked (affected non-empty) => the rollback branch (moved===0 && affected.length>0) runs.
    chrome.__state.moveError = "Tabs cannot be edited right now (user may be dragging a tab).";
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { items: [{ tabId: 100 }, { tabId: 300 }], windowId: 3 }),
      ctx({ map }),
    );
    expect(res.result.results.every((r) => r.ok === false)).toBe(true);
    // moved===0 but the target (3) + source (1) windows were marked => rollback with those.
    expect(map.clearCuratorCause).toHaveBeenCalledTimes(1);
    expect(new Set(map.clearCuratorCause.mock.calls[0][0])).toEqual(new Set([3, 1]));
  });

  it("an ineligible/vanished shared target fails EVERY item with no_window, sends no move", async () => {
    threeWindowsWithPinned();
    const move = vi.spyOn(chrome.tabs, "move");
    const res = await dispatchCommand(
      frame(CMD_MOVE_TAB, { items: [{ tabId: 100 }, { tabId: 300 }], windowId: 42 }),
      ctx(),
    );
    expect(res.result.results.every((r) => r.error === "no_window")).toBe(true);
    expect(move).not.toHaveBeenCalled();
  });
});

describe("#49 bulk open_tab {items} + hoisted-out-of-loop work", () => {
  it("opens each item, seeds each, and picks the §9 window ONCE for the whole list", async () => {
    globalThis.chrome = createChromeMock({
      tabs: [{ id: 1, windowId: 7, url: "https://x/" }],
      windows: [{ id: 7, type: "normal", state: "normal" }],
      lastFocused: { id: 7, focused: true },
    });
    const map = spyMap();
    const res = await dispatchCommand(
      frame(CMD_OPEN_TAB, {
        items: [
          { url: "https://one/", seed_age_ms: 1000 },
          { url: "javascript:bad", seed_age_ms: 0 }, // rejected at the edge
          { url: "https://two/", seed_age_ms: 2000 },
        ],
      }),
      ctx({ map }),
    );
    expect(res.ok).toBe(true);
    expect(res.result.results[0]).toMatchObject({ index: 0, ok: true, windowId: 7 });
    expect(res.result.results[1]).toMatchObject({ index: 1, ok: false, error: "precondition_failed" });
    expect(res.result.results[2]).toMatchObject({ index: 2, ok: true, windowId: 7 });
    // ONE batched seed for the whole list, carrying only the two valid urls.
    expect(map.seedCuratorTab).not.toHaveBeenCalled();
    expect(map.seedCuratorTabs).toHaveBeenCalledTimes(1);
    expect(map.seedCuratorTabs.mock.calls[0][0]).toHaveLength(2);
  });

  it("acceptance 10 (open): a 20-item open seeds the map ONCE, not per item", async () => {
    // The sibling of the close-path budget test, for the path bulk RELOCATE actually
    // uses. seedCuratorTab is a full map load+save each; doing it per item re-imports
    // the per-element cost that one frame per list was bought to remove. Reddens if the
    // seed goes back inside the loop.
    globalThis.chrome = createChromeMock({
      tabs: [],
      windows: [{ id: 7, type: "normal", state: "normal" }],
      lastFocused: { id: 7, focused: true },
    });
    const map = spyMap();
    const items = [];
    for (let i = 0; i < 20; i += 1) items.push({ url: `https://x/${i}`, seed_age_ms: i });

    const res = await dispatchCommand(frame(CMD_OPEN_TAB, { items }), ctx({ map }));

    expect(res.ok).toBe(true);
    expect(res.result.results).toHaveLength(20);
    expect(res.result.results.every((r) => r.ok)).toBe(true);
    expect(map.seedCuratorTab).not.toHaveBeenCalled();
    expect(map.seedCuratorTabs).toHaveBeenCalledTimes(1);
    expect(map.seedCuratorTabs.mock.calls[0][0]).toHaveLength(20);
  });

  it("acceptance 10: a 20-item close on a 200-tab map does O(1) hoisted work, not O(n)", async () => {
    // 200 live tabs across 10 windows; close 20 of them in ONE list frame. The proof that
    // the per-item work is hoisted: chrome.tabs.query runs ONCE (not one get per item),
    // getLastFocused ONCE, markCuratorCause ONCE — so a 200-tab map is not loaded+saved 20
    // times and the cmd_timeout_ms budget holds.
    const tabs = [];
    for (let i = 0; i < 200; i += 1) {
      tabs.push({ id: i + 1, windowId: (i % 10) + 1, url: `https://t/${i}`, pinned: false, active: false, audible: false });
    }
    const windows = [];
    for (let w = 1; w <= 10; w += 1) windows.push({ id: w, type: "normal", state: "normal" });
    globalThis.chrome = createChromeMock({ tabs, windows, lastFocused: { id: 1, focused: true } });
    const query = vi.spyOn(chrome.tabs, "query");
    const getLF = vi.spyOn(chrome.windows, "getLastFocused");
    const get = vi.spyOn(chrome.tabs, "get");
    const map = spyMap();
    const items = [];
    for (let i = 0; i < 20; i += 1) items.push({ tabId: i * 5 + 1 }); // 20 spread-out ids

    const started = Date.now();
    const res = await dispatchCommand(frame(CMD_CLOSE_TAB, { items }), ctx({ map }));
    const elapsed = Date.now() - started;

    expect(res.result.results).toHaveLength(20);
    expect(res.result.results.every((r) => r.ok)).toBe(true);
    expect(query).toHaveBeenCalledTimes(1); // ONE query, not 20 per-item gets
    expect(getLF).toHaveBeenCalledTimes(1);
    expect(get).not.toHaveBeenCalled();
    expect(map.markCuratorCause).toHaveBeenCalledTimes(1);
    expect(map.readMap).not.toHaveBeenCalled(); // no minIdleMs => no map read at all
    expect(elapsed).toBeLessThan(1000); // trivially inside any cmd_timeout_ms budget
  });
});

// --- the injected function BODIES -------------------------------------------
//
// These are tested DIRECTLY, not through dispatchCommand: the chrome mock's
// scripting.executeScript returns a canned value and never RUNS the function, so nothing
// else in this file can prove what the code injected into a real page actually does.

describe("evalInWorld — execute_js's injected body", () => {
  it("proves the bug AND the fix: indirect eval cannot return; the async path can", async () => {
    // THE bug this wave fixes. Indirect eval supports neither a top-level `return`
    // (SyntaxError) nor top-level `await`, so async code could never hand a value back —
    // it arrived as null. `chrome.scripting` DOES await a promise the injected function
    // returns; the eval wrapper was the whole problem.
    expect(() => evalInWorld("return 7;", false)).toThrow(SyntaxError);
    await expect(evalInWorld("return await Promise.resolve(7);", true)).resolves.toBe(7);
    await expect(
      evalInWorld("const v = await Promise.resolve(2); return v * 3;", true),
    ).resolves.toBe(6);
  });

  it("a source whose LAST LINE is a // comment still runs on BOTH compile paths", async () => {
    // Why AsyncFunction and not `new Function("(async()=>{" + source + "})()")`: with
    // string splicing the appended `})()` lands INSIDE that trailing comment and the whole
    // thing is a SyntaxError. Go back to splicing and this reddens.
    //
    // The same hazard reappears one layer down in the EXPRESSION compile
    // (`return (<source>\n);`), which is why the newline before `)` is there. Both paths
    // must survive it, so exercise both: the first falls back to the statement body, the
    // second is compiled as an expression.
    await expect(evalInWorld("return 42; // the answer", true)).resolves.toBe(42);
    await expect(evalInWorld("40 + 2 // the answer", true)).resolves.toBe(42);
  });

  it("a PROMISE is chained, not cloned — the default path keeps working", async () => {
    // chrome.scripting awaits a promise the injected function returns, so this has ALWAYS
    // worked with no flag. Running the clone probe first breaks it: structuredClone throws
    // DataCloneError on a promise, and the agent gets {__unserializable:"Promise"} instead
    // of its data. Omitting a new parameter must reproduce pre-wave behaviour exactly.
    await expect(evalInWorld("Promise.resolve(7)", false)).resolves.toBe(7);
    await expect(
      evalInWorld("Promise.resolve({ ok: true }).then((v) => v)", false),
    ).resolves.toEqual({ ok: true });
  });

  it("the probe still applies to what the promise RESOLVES TO", async () => {
    // Chaining must not disable the diagnostic — it must MOVE it onto the value that
    // actually crosses the structured-clone boundary.
    const got = await evalInWorld("Promise.resolve(() => 1)", false);
    expect(got.__unserializable).toBe("Function");
  });

  it("with await_promise ON, a plain EXPRESSION still returns its value", async () => {
    // `new AsyncFunction(source)` makes the source the function BODY, which discards an
    // expression's completion value — so this answered null: the very silent-null failure
    // the flag exists to remove, and one an agent that turns it on by default would hit
    // everywhere. Hence the expression-first compile.
    await expect(evalInWorld("2 + 2", true)).resolves.toBe(4);
    await expect(evalInWorld("({ a: 1 })", true)).resolves.toEqual({ a: 1 });
    await expect(evalInWorld("await Promise.resolve(9)", true)).resolves.toBe(9);
    // …and multi-statement code still reaches the statement fallback, where `return` works.
    await expect(
      evalInWorld("const v = await Promise.resolve(2);\nreturn v * 3;", true),
    ).resolves.toBe(6);
  });

  it("the sync path is unchanged for an ordinary value", () => {
    expect(evalInWorld("2 + 2", false)).toBe(4);
    expect(evalInWorld("({a: 1})", false)).toEqual({ a: 1 });
  });

  it("an UNSERIALIZABLE value names itself instead of arriving as null", () => {
    // chrome.scripting structured-clones the result out of the page, so a function / DOM
    // node / Symbol silently becomes null — which reads exactly like "the code returned
    // null" and is this verb's most confusing failure.
    const fn = evalInWorld("(function widget() {})", false);
    expect(fn.__unserializable).toBe("Function");
    expect(fn.preview).toContain("widget");

    const obj = evalInWorld("({ handler: function () {} })", false);
    expect(obj.__unserializable).toBe("Object");

    const sym = evalInWorld("Symbol('x')", false);
    expect(sym.__unserializable).toBe("Symbol");
  });

  it("the diagnostic applies to the async path too", async () => {
    const got = await evalInWorld("return () => 1;", true);
    expect(got.__unserializable).toBe("Function");
  });

  it("a page that deleted structuredClone gets today's behaviour, not a fabrication", () => {
    // MAIN world shares the page's globals. Without the guard the probe would throw for
    // EVERY value and mark each one unserializable — a diagnostic that invents its finding
    // is worse than none.
    const saved = globalThis.structuredClone;
    globalThis.structuredClone = undefined;
    try {
      expect(evalInWorld("({a: 1})", false)).toEqual({ a: 1 });
    } finally {
      globalThis.structuredClone = saved;
    }
  });
});

// A minimal document double for the two DOM-reading injected bodies.
function withDocument(doc, fn) {
  const saved = globalThis.document;
  globalThis.document = doc;
  try {
    return fn();
  } finally {
    globalThis.document = saved;
  }
}

// `badSelectors` lists selectors the double should reject the way a real engine does — by
// throwing a SyntaxError — so the "a typo must not burn the whole budget" tests exercise
// the real code path rather than a stubbed return value.
function fakeDoc(bodyText, matches = {}, badSelectors = []) {
  return {
    body: bodyText === null ? null : { innerText: bodyText },
    querySelector: (sel) => {
      if (badSelectors.includes(sel)) {
        const e = new Error(`'${sel}' is not a valid selector`);
        e.name = "SyntaxError";
        throw e;
      }
      return sel in matches ? matches[sel] : null;
    },
  };
}

describe("readTextInWorld — get_text's injected body", () => {
  it("reads the body with no selector, and the element with one", () => {
    withDocument(fakeDoc("whole page", { "#main": { innerText: "just main" } }), () => {
      expect(readTextInWorld(null, null)).toEqual({
        found: true, text: "whole page", totalBytes: 10,
      });
      expect(readTextInWorld("#main", null)).toEqual({
        found: true, text: "just main", totalBytes: 9,
      });
    });
  });

  it("a selector that matches NOTHING is found:false, never an empty string", () => {
    // "your selector is wrong" and "the page is blank" are different facts; conflating
    // them sends the agent to debug the wrong one.
    withDocument(fakeDoc("whole page"), () => {
      expect(readTextInWorld("#nope", null)).toEqual({ found: false, text: "", totalBytes: 0 });
    });
    // A matched-but-genuinely-empty element still reports found:true.
    withDocument(fakeDoc("x", { "#empty": { innerText: "" } }), () => {
      expect(readTextInWorld("#empty", null)).toEqual({ found: true, text: "", totalBytes: 0 });
    });
  });

  it("cuts at maxBytes on a CHARACTER boundary and reports the TRUE total", () => {
    // Ten 2-byte characters = 20 bytes. A 5-byte cut lands mid-character: the half
    // sequence is dropped rather than decoded into U+FFFD.
    withDocument(fakeDoc("Ω".repeat(10)), () => {
      const got = readTextInWorld(null, 5);
      expect(got.text).toBe("ΩΩ"); // 4 bytes kept, the severed 5th dropped
      expect(got.text).not.toContain("�");
      expect(got.truncated).toBe(true);
      expect(got.totalBytes).toBe(20); // the size of the WHOLE document, not of the cut
    });
  });

  it("does not cut when the text fits, and ignores a zero/absent maxBytes", () => {
    withDocument(fakeDoc("short"), () => {
      expect(readTextInWorld(null, 1000).truncated).toBeUndefined();
      expect(readTextInWorld(null, 0)).toEqual({ found: true, text: "short", totalBytes: 5 });
    });
  });
});

describe("matchInWorld — the wait_for predicate", () => {
  it("answers the selector question", () => {
    withDocument(fakeDoc("", { ".done": {} }), () => {
      expect(matchInWorld(".done", null)).toEqual({ matched: true });
      expect(matchInWorld(".missing", null)).toEqual({ matched: false });
    });
  });

  it("answers the textContains question", () => {
    withDocument(fakeDoc("Order complete, thank you"), () => {
      expect(matchInWorld(null, "complete")).toEqual({ matched: true });
      expect(matchInWorld(null, "failed")).toEqual({ matched: false });
    });
  });

  it("reports a MALFORMED selector as a value, never a throw", () => {
    // A throw is read by pollUntil as "the frame is being recreated" and polled through to
    // the deadline — so a typo would cost the whole budget and then answer "not matched".
    // The distinction has to travel as data.
    withDocument(fakeDoc("", {}, ["#a:has(>"]), () => {
      const got = matchInWorld("#a:has(>", null);
      expect(got.badSelector).toBe(true);
      expect(got.matched).toBeUndefined();
      expect(got.message).toMatch(/not a valid selector/);
    });
  });
});

// --- get_text: the FIXED-function read (§12) ---------------------------------
function chromeWithOneTab(url = "https://x/", extra = {}) {
  globalThis.chrome = createChromeMock({ tabs: [{ id: 5, windowId: 1, url, ...extra }] });
}

describe("get_text", () => {
  it("runs with the execute_js checkbox OFF — a fixed function is not eval (§12)", async () => {
    // THE load-bearing assertion of this verb. The execute_js gate exists because
    // ARBITRARY code arrives there and truncated code cannot be reconstructed; a function
    // committed into this bundle has nothing to reconstruct. Gate this on the checkbox and
    // the whole point of the verb (a cheap read that does not need the dangerous switch)
    // is gone.
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { found: true, text: "hello", totalBytes: 5 } }];
    const res = await dispatchCommand(frame(CMD_GET_TEXT, { tabId: 5 }), ctx());
    expect(res).toEqual({ ok: true, result: { text: "hello" } });
  });

  it("passes selector + maxBytes to the FIXED function, never a code string", async () => {
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { found: true, text: "t", totalBytes: 1 } }];
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    await dispatchCommand(frame(CMD_GET_TEXT, { tabId: 5, selector: "#a", maxBytes: 9 }), ctx());
    const injection = exec.mock.calls[0][0];
    expect(injection.args).toEqual(["#a", 9]);
    expect(typeof injection.func).toBe("function");
  });

  it("surfaces the extension-side truncation with the TRUE total", async () => {
    chromeWithOneTab();
    chrome.__state.scriptResults = [
      { result: { found: true, text: "cut", totalBytes: 4096, truncated: true } },
    ];
    const res = await dispatchCommand(frame(CMD_GET_TEXT, { tabId: 5, maxBytes: 3 }), ctx());
    // camelCase on the WIRE, like every other §6 key (`tabId`, `elapsedMs`); the MCP layer
    // is what renames it to `total_bytes` for the agent.
    expect(res.result).toEqual({ text: "cut", truncated: true, totalBytes: 4096 });
  });

  it("a MALFORMED selector is precondition_failed, not `internal`", async () => {
    // Without readTextInWorld catching it, the SyntaxError escapes the injection and the
    // dispatcher's outer try turns it into `internal` — a code that says "our bug" and
    // sends the agent to read our logs instead of its own selector.
    chromeWithOneTab();
    chrome.__state.scriptResults = [
      { result: { found: false, badSelector: true, message: "'#a:has(>' is not a valid selector" } },
    ];
    const res = await dispatchCommand(
      frame(CMD_GET_TEXT, { tabId: 5, selector: "#a:has(>" }), ctx(),
    );
    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("precondition_failed");
    expect(res.error.message).toContain("#a:has(>");
  });

  it("a selector that matched nothing => precondition_failed", async () => {
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { found: false, text: "", totalBytes: 0 } }];
    const res = await dispatchCommand(frame(CMD_GET_TEXT, { tabId: 5, selector: "#nope" }), ctx());
    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("precondition_failed");
    expect(res.error.message).toContain("#nope");
  });

  it("keeps execute_js's http/https target guard (§12)", async () => {
    // With <all_urls> granted an unguarded read would return a file:// page's text.
    chromeWithOneTab("file:///etc/passwd");
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const res = await dispatchCommand(frame(CMD_GET_TEXT, { tabId: 5 }), ctx());
    expect(res.error.code).toBe("precondition_failed");
    expect(exec).not.toHaveBeenCalled();
  });

  it("a vanished tab is no_such_tab, not internal", async () => {
    chromeWithOneTab();
    const res = await dispatchCommand(frame(CMD_GET_TEXT, { tabId: 999 }), ctx());
    expect(res.error.code).toBe("no_such_tab");
  });
});

// --- wait_for ----------------------------------------------------------------
// A fake clock whose SLEEP is what advances time: the poll loop then runs to its deadline
// instantly and deterministically, instead of spending real seconds.
function fakeClock(start = NOW) {
  const state = { t: start, sleeps: 0, hooks: [] };
  return {
    now: () => state.t,
    sleep: async (ms) => {
      state.t += ms;
      state.sleeps += 1;
      for (const h of state.hooks) h(state.sleeps);
    },
    onSleep: (fn) => state.hooks.push(fn),
    get sleeps() {
      return state.sleeps;
    },
  };
}

describe("wait_for", () => {
  it("requires EXACTLY ONE predicate and polls NOTHING otherwise", async () => {
    chromeWithOneTab();
    const get = vi.spyOn(chrome.tabs, "get");
    for (const params of [
      { tabId: 5, timeoutMs: 1000 }, // none
      { tabId: 5, timeoutMs: 1000, urlMatches: "a", selector: "#b" }, // two
      { tabId: 5, timeoutMs: 1000, urlMatches: "a", selector: "#b", textContains: "c" },
    ]) {
      const res = await dispatchCommand(frame(CMD_WAIT_FOR, params), ctx());
      expect(res.ok).toBe(false);
      expect(res.error.code).toBe("precondition_failed");
      expect(res.error.message).toMatch(/EXACTLY ONE/);
    }
    expect(get).not.toHaveBeenCalled(); // refused before touching the browser
  });

  it("urlMatches needs NO injection at all and matches a substring", async () => {
    chromeWithOneTab("https://shop/checkout/done?x=1");
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const c = fakeClock();
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, urlMatches: "/checkout/done", timeoutMs: 5000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res).toEqual({ ok: true, result: { matched: true, elapsedMs: 0 } });
    expect(exec).not.toHaveBeenCalled();
  });

  it("keeps polling until the condition becomes true, then reports elapsedMs", async () => {
    chromeWithOneTab("https://shop/cart");
    const c = fakeClock();
    // The page "navigates" on the second poll interval.
    c.onSleep((n) => {
      if (n === 2) chrome.__state.tabs[0].url = "https://shop/done";
    });
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, urlMatches: "/done", timeoutMs: 5000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(true);
    expect(res.result.matched).toBe(true);
    expect(res.result.elapsedMs).toBe(2 * WAIT_POLL_MS);
  });

  it("a condition that never holds is a SUCCESS with matched:false, NOT `timeout`", async () => {
    // §11 reserves `timeout` for "no frame arrived" — state UNKNOWN, do not blindly retry.
    // A wait that ran its full course is the opposite fact: the browser answered, and the
    // answer is "no". Spelling both as one error code destroys the distinction the agent
    // needs; spelling this one as a verdict puts it in the response SHAPE, which survives
    // the wire (an `elapsedMs` on an error frame would not — the service discards `result`
    // on any ok:false).
    chromeWithOneTab("https://shop/cart");
    const c = fakeClock();
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, urlMatches: "/never", timeoutMs: 1000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ matched: false, elapsedMs: 1000 });
    // It really polled to the deadline rather than giving up at once (1000/250 = 4).
    expect(c.sleeps).toBe(4);
  });

  it("a MALFORMED selector is refused at once, not polled to the deadline", async () => {
    // `document.querySelector("#a:has(>")` throws SyntaxError, and pollUntil reads a throw
    // from an injection as "the frame is being recreated". Untreated, a typo costs the
    // whole 30-60 s budget and THEN reads as "condition not met" — the agent debugs the
    // page instead of its selector.
    chromeWithOneTab();
    chrome.__state.scriptResults = [
      { result: { badSelector: true, message: "'#a:has(>' is not a valid selector" } },
    ];
    const c = fakeClock();
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, selector: "#a:has(>", timeoutMs: 60000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("precondition_failed");
    expect(res.error.message).toContain("#a:has(>");
    expect(c.sleeps).toBe(0); // refused on the FIRST probe
  });

  it("re-checks the scheme on EVERY poll, not once up front", async () => {
    // The guard is checked before the first probe, but a wait keeps injecting for up to a
    // minute and the page can move under us. With <all_urls> granted, an injection into a
    // file:// page reads it same-origin — so a guard that expires mid-wait is not a guard.
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { matched: false } }];
    const c = fakeClock();
    c.onSleep((n) => {
      if (n === 1) chrome.__state.tabs[0].url = "file:///etc/passwd";
    });
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, selector: ".ready", timeoutMs: 60000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(false);
    expect(res.error.code).toBe("precondition_failed");
    expect(c.sleeps).toBe(1); // caught on the second probe, not at the deadline
  });

  it("selector polls the FIXED predicate in the page", async () => {
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { matched: false } }];
    const c = fakeClock();
    c.onSleep((n) => {
      if (n === 1) chrome.__state.scriptResults = [{ result: { matched: true } }];
    });
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, selector: ".ready", timeoutMs: 5000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.result).toEqual({ matched: true, elapsedMs: WAIT_POLL_MS });
    expect(exec.mock.calls[0][0].args).toEqual([".ready", null]);
    expect(exec.mock.calls[0][0].func).toBe(matchInWorld);
  });

  it("textContains rides the same predicate with the other argument", async () => {
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { matched: true } }];
    const exec = vi.spyOn(chrome.scripting, "executeScript");
    const c = fakeClock();
    await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, textContains: "Paid", timeoutMs: 5000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(exec.mock.calls[0][0].args).toEqual([null, "Paid"]);
  });

  it("runs with the execute_js checkbox OFF (fixed function, §12)", async () => {
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { matched: true } }];
    const c = fakeClock();
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, selector: ".x", timeoutMs: 1000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(true);
  });

  it("guards the scheme for the INJECTING predicates but not for urlMatches", async () => {
    // A tab mid-navigation legitimately sits on about:blank, and waiting for it to REACH
    // an http url is the main use of urlMatches — guarding it would make the verb useless.
    chromeWithOneTab("about:blank");
    const c = fakeClock();
    const injected = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, selector: ".x", timeoutMs: 1000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(injected.error.code).toBe("precondition_failed");

    const byUrl = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, urlMatches: "about:", timeoutMs: 1000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(byUrl.ok).toBe(true);
  });

  it("a tab that vanishes mid-wait ends the wait with no_such_tab", async () => {
    chromeWithOneTab();
    chrome.__state.scriptResults = [{ result: { matched: false } }];
    const c = fakeClock();
    c.onSleep((n) => {
      if (n === 1) chrome.__state.tabs.length = 0; // the human closed it
    });
    const res = await dispatchCommand(
      frame(CMD_WAIT_FOR, { tabId: 5, selector: ".x", timeoutMs: 5000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.error.code).toBe("no_such_tab");
  });

  it("refuses a missing or non-positive timeoutMs loudly", async () => {
    chromeWithOneTab();
    for (const timeoutMs of [undefined, 0, -1, "5000", 1.5]) {
      const res = await dispatchCommand(
        frame(CMD_WAIT_FOR, { tabId: 5, urlMatches: "x", timeoutMs }),
        ctx(),
      );
      expect(res.error.code).toBe("precondition_failed");
    }
  });
});

// --- navigate_tab waitUntil ---------------------------------------------------
describe("navigate_tab waitUntil", () => {
  it("DEFAULT (absent) is byte-for-byte today's behaviour: update, {ok:true}, no wait", async () => {
    // The reset path (src/api/rules.py) calls this verb; it must not change at all.
    chromeWithOneTab("https://old/", { status: "complete" });
    const c = fakeClock();
    const res = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, { tabId: 5, url: "https://new/" }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res).toEqual({ ok: true, result: { ok: true } });
    expect(c.sleeps).toBe(0); // nothing was waited for
    expect(chrome.__state.tabs[0].url).toBe("https://new/");
  });

  it("waitUntil:'none' is explicitly the same as absent", async () => {
    chromeWithOneTab("https://old/", { status: "complete" });
    const c = fakeClock();
    const res = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, { tabId: 5, url: "https://new/", waitUntil: "none" }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res).toEqual({ ok: true, result: { ok: true } });
    expect(c.sleeps).toBe(0);
  });

  it("'load' waits for status:'complete' and never accepts the OLD page's complete", async () => {
    chromeWithOneTab("https://old/", { status: "complete" });
    const c = fakeClock();
    // The tab is 'complete' from the PREVIOUS page at update time. Accepting that would
    // return before the new document even started loading — the exact bug the option
    // exists to prevent — so the first check happens only after one poll interval.
    c.onSleep((n) => {
      if (n === 1) chrome.__state.tabs[0].status = "loading";
      if (n === 2) chrome.__state.tabs[0].status = "complete";
    });
    const res = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, { tabId: 5, url: "https://new/", waitUntil: "load", timeoutMs: 5000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(true);
    expect(res.result.elapsedMs).toBe(2 * WAIT_POLL_MS);
  });

  it("'selector' waits for the fixed predicate", async () => {
    chromeWithOneTab("https://old/", { status: "complete" });
    chrome.__state.scriptResults = [{ result: { matched: false } }];
    const c = fakeClock();
    c.onSleep((n) => {
      if (n === 2) chrome.__state.scriptResults = [{ result: { matched: true } }];
    });
    const res = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, {
        tabId: 5, url: "https://new/", waitUntil: "selector", selector: "#app", timeoutMs: 5000,
      }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(true);
    expect(res.result.elapsedMs).toBe(2 * WAIT_POLL_MS);
  });

  it("a wait that expires is ok+matched:false — the navigation DID happen", async () => {
    // Same rule as wait_for: the deadline passing is a VERDICT, not an error. `timeout`
    // means "no frame arrived, state unknown", and here the state is perfectly known — the
    // tab really moved, the condition just never came true.
    chromeWithOneTab("https://old/", { status: "complete" });
    const c = fakeClock();
    c.onSleep(() => {
      chrome.__state.tabs[0].status = "loading"; // never finishes
    });
    const res = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, { tabId: 5, url: "https://new/", waitUntil: "load", timeoutMs: 1000 }),
      ctx({ now: c.now, sleep: c.sleep }),
    );
    expect(res.ok).toBe(true);
    expect(res.result).toEqual({ ok: true, matched: false, elapsedMs: 1000 });
    expect(chrome.__state.tabs[0].url).toBe("https://new/"); // it really navigated
  });

  it("a bad waitUntil / missing selector is refused BEFORE the tab is navigated", async () => {
    // A refusal AFTER the update would read as "nothing happened" about a tab that has
    // already moved.
    chromeWithOneTab("https://old/", { status: "complete" });
    const bad = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, { tabId: 5, url: "https://new/", waitUntil: "settled" }),
      ctx(),
    );
    expect(bad.error.code).toBe("precondition_failed");
    const noSelector = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, {
        tabId: 5, url: "https://new/", waitUntil: "selector", timeoutMs: 100,
      }),
      ctx(),
    );
    expect(noSelector.error.code).toBe("precondition_failed");
    expect(chrome.__state.tabs[0].url).toBe("https://old/"); // untouched
  });

  it("still refuses a non-http url before anything else (§12)", async () => {
    chromeWithOneTab("https://old/", { status: "complete" });
    const res = await dispatchCommand(
      frame(CMD_NAVIGATE_TAB, {
        tabId: 5, url: "javascript:alert(1)", waitUntil: "load", timeoutMs: 100,
      }),
      ctx(),
    );
    expect(res.error.code).toBe("precondition_failed");
    expect(chrome.__state.tabs[0].url).toBe("https://old/");
  });
});
