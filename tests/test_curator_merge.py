"""Step-9 window-merge tests (§9) — the pure guards plus one end-to-end pass.

The pure ``decide_window_merges`` planner is unit- and mutation-tested against a
frozen :class:`Mirror`: every §9 guard is written so that DROPPING it reddens the
named test (on-screen / audible / idle / fullscreen / target choice / pinned trap /
two-window minimum). One integration test drives a real ``run_pass`` through a tiny
extension emulator to prove the ``merge_windows`` command is issued, the NON-undoable
``window_merge`` row is journalled, and the pass never re-stamps the merged tabs'
ages (§17 acceptance: on-screen-not-merged, pinned-stay/unpinned-leave, ages-not-reset).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from src.api.undo import _classify
from src.curator import runner
from src.curator.decide import WindowMerge, decide_window_merges
from src.curator.mirror import InstanceRow, Mirror, TabRow
from src.db.access import Database
from src.ext import channel, protocol
from src.ext.commands import resolve_response
from src.ext.registry import ConnState, Registry

HOUR = 3_600_000
NOW = 10_000_000
IDLE = HOUR  # IDLE_MINUTES=60


# --- pure-planner fixtures ---------------------------------------------------
def _tab(instance_id, tab_id, *, window_id, last_active_at=None, pinned=0, active=0,
         audible=0, age_unknown=0, url="https://x/"):
    if last_active_at is None:
        last_active_at = NOW - 2 * HOUR  # idle for two hours (well past IDLE)
    return TabRow(
        instance_id=instance_id, tab_id=tab_id, window_id=window_id, url=url,
        title="t", pinned=pinned, active=active, audible=audible,
        opened_at=0, last_active_at=last_active_at, age_unknown=age_unknown,
    )


def _inst(instance_id, *, focused_window_id=None):
    return InstanceRow(
        id=instance_id, connected=1, focused_window_id=focused_window_id,
        session_id="s", snapshot_at=NOW, conn_epoch=1,
    )


def _mirror(tabs, instances, windows):
    return Mirror(
        tabs=list(tabs), instances={i.id: i for i in instances}, windows=dict(windows),
    )


def _plan(mirror, ready, main_instance_id="main"):
    return decide_window_merges(
        mirror, set(ready), now=NOW, idle_ms=IDLE, main_instance_id=main_instance_id
    )


# --- main is exempt from automatic window merge (§9, owner decision 2026-08-06) --
def test_main_windows_are_not_auto_merged_but_thematic_still_are():
    # main and a thematic instance each have two mergeable, all-idle windows. In the
    # SAME plan main is skipped entirely while the thematic instance folds normally.
    tabs = [
        _tab("main", 10, window_id=1), _tab("main", 20, window_id=2),
        _tab("prox", 30, window_id=1), _tab("prox", 40, window_id=2),
    ]
    windows = {
        ("main", 1): ("normal", "normal"), ("main", 2): ("normal", "normal"),
        ("prox", 1): ("normal", "normal"), ("prox", 2): ("normal", "normal"),
    }
    plans = _plan(
        _mirror(tabs, [_inst("main"), _inst("prox")], windows),
        {"main", "prox"},
    )
    assert all(m.instance_id != "main" for m in plans)  # main untouched, both windows live
    prox = [m for m in plans if m.instance_id == "prox"]
    assert len(prox) == 1  # thematic instance still merges in the same pass


def test_main_skip_is_keyed_on_the_configured_id():
    # The SAME main-shaped mirror, but main_instance_id points elsewhere -> "main" is
    # now an ordinary instance and DOES get a merge. Proves the skip is the exemption,
    # not some unrelated reason the two windows failed to merge.
    tabs = [_tab("main", 10, window_id=1), _tab("main", 20, window_id=2)]
    windows = {("main", 1): ("normal", "normal"), ("main", 2): ("normal", "normal")}
    mirror = _mirror(tabs, [_inst("main")], windows)
    assert _plan(mirror, {"main"}, main_instance_id="main") == []
    [m] = _plan(mirror, {"main"}, main_instance_id="somethingelse")
    assert m.instance_id == "main"


# --- target choice: most tabs; tie -> smallest window_id (§9) ----------------
def test_target_is_window_with_most_tabs():
    # Window 2 has two tabs, window 1 one => target is 2; window 1 folds into it.
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 20, window_id=2),
        _tab("a", 21, window_id=2),
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    [m] = _plan(_mirror(tabs, [_inst("a")], windows), {"a"})
    assert m.target_window_id == 2
    assert m.source_window_ids == [1]
    assert m.moved_tab_ids == [10]


def test_target_tie_breaks_to_smallest_window_id():
    # Both windows have one tab => tie -> target is the smaller id (1); 2 folds in.
    tabs = [_tab("a", 10, window_id=1), _tab("a", 20, window_id=2)]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    [m] = _plan(_mirror(tabs, [_inst("a")], windows), {"a"})
    assert m.target_window_id == 1
    assert m.source_window_ids == [2]


# --- ACCEPTANCE §17: a window with an on-screen tab is NOT merged ------------
def test_on_screen_tab_blocks_its_window_as_source():
    # Window 2 is the FOCUSED window and its tab is active => on screen. Even though
    # it is idle-old, it must NOT be folded away. Drop the on-screen guard and the
    # source list gains window 2.
    tabs = [
        _tab("a", 10, window_id=1),           # target (most tabs after guard)
        _tab("a", 11, window_id=1),
        _tab("a", 20, window_id=2, active=1),  # active in the focused window => on screen
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    mirror = _mirror(tabs, [_inst("a", focused_window_id=2)], windows)
    assert _plan(mirror, {"a"}) == []  # window 2 not a source; window 1 has no other source


def test_active_tab_in_UNfocused_window_does_not_block():
    # `active` alone is not "on screen" — an unfocused instance's last-viewed tab is
    # active forever (§5). Window 2's tab is active but its window is NOT focused, so
    # window 2 still folds into window 1.
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 11, window_id=1),
        _tab("a", 20, window_id=2, active=1),
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    mirror = _mirror(tabs, [_inst("a", focused_window_id=1)], windows)
    [m] = _plan(mirror, {"a"})
    assert m.source_window_ids == [2] and m.moved_tab_ids == [20]


# --- audible guard ----------------------------------------------------------
def test_audible_tab_blocks_its_window_as_source():
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 11, window_id=1),
        _tab("a", 20, window_id=2, audible=1),  # background media => not a source
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    assert _plan(_mirror(tabs, [_inst("a")], windows), {"a"}) == []


# --- idle guard -------------------------------------------------------------
def test_any_fresh_tab_blocks_the_window():
    # Window 2 has one hour-idle tab and one FRESH tab => not ALL idle => not a source.
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 11, window_id=1),
        _tab("a", 20, window_id=2),
        _tab("a", 21, window_id=2, last_active_at=NOW - 60_000),  # 1 min ago: fresh
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    assert _plan(_mirror(tabs, [_inst("a")], windows), {"a"}) == []


def test_all_idle_window_is_a_source():
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 11, window_id=1),
        _tab("a", 20, window_id=2),
        _tab("a", 21, window_id=2),
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    [m] = _plan(_mirror(tabs, [_inst("a")], windows), {"a"})
    assert m.source_window_ids == [2] and sorted(m.moved_tab_ids) == [20, 21]


# --- fullscreen / non-normal: neither source nor target ---------------------
def test_fullscreen_window_is_neither_source_nor_target():
    # Window 2 is fullscreen (a showcase): it is NOT a source. With only window 1
    # left mergeable there is no second window, so nothing merges.
    tabs = [_tab("a", 10, window_id=1), _tab("a", 20, window_id=2)]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "fullscreen")}
    assert _plan(_mirror(tabs, [_inst("a")], windows), {"a"}) == []


def test_popup_window_is_never_target_or_source():
    # A popup with the MOST tabs must not become the target and its tabs must not fold.
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 20, window_id=2),  # popup, two tabs
        _tab("a", 21, window_id=2),
        _tab("a", 30, window_id=3),
    ]
    windows = {
        ("a", 1): ("normal", "normal"),
        ("a", 2): ("popup", "normal"),
        ("a", 3): ("normal", "normal"),
    }
    [m] = _plan(_mirror(tabs, [_inst("a")], windows), {"a"})
    # Target is a NORMAL window (tie 1 vs 3 -> smaller id 1); window 3 folds; popup 2 ignored.
    assert m.target_window_id == 1
    assert m.source_window_ids == [3] and m.moved_tab_ids == [30]


def test_maximized_window_is_mergeable_fork():
    # FORK (§9 "не fullscreen"): a maximized, hour-idle window IS a valid source —
    # matching step4_passes, which lets that window's tabs relocate. Only fullscreen
    # is exempt. Window 2 (maximized) folds into window 1.
    tabs = [_tab("a", 10, window_id=1), _tab("a", 11, window_id=1), _tab("a", 20, window_id=2)]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "maximized")}
    [m] = _plan(_mirror(tabs, [_inst("a")], windows), {"a"})
    assert m.source_window_ids == [2] and m.moved_tab_ids == [20]


# --- ACCEPTANCE §17: pinned stay, unpinned leave ----------------------------
def test_pinned_tabs_excluded_from_move_window_still_a_source():
    # Window 2: one pinned + one unpinned, both idle. Only the unpinned tab is listed
    # to move (the extension enforces the same); the window is still a source because
    # it has an unpinned tab, but the pinned one keeps it from truly vanishing.
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 11, window_id=1),
        _tab("a", 20, window_id=2, pinned=1),
        _tab("a", 21, window_id=2, pinned=0),
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    [m] = _plan(_mirror(tabs, [_inst("a")], windows), {"a"})
    assert m.source_window_ids == [2]
    assert m.moved_tab_ids == [21]  # NOT 20 (pinned)


def test_only_pinned_window_is_not_a_source():
    # Window 2 is all-pinned: nothing may move, so it is not a source at all (no
    # empty merge command for it).
    tabs = [
        _tab("a", 10, window_id=1),
        _tab("a", 11, window_id=1),
        _tab("a", 20, window_id=2, pinned=1),
        _tab("a", 21, window_id=2, pinned=1),
    ]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    assert _plan(_mirror(tabs, [_inst("a")], windows), {"a"}) == []


# --- fewer than two mergeable windows => no merge ---------------------------
def test_single_window_instance_no_merge():
    tabs = [_tab("a", 10, window_id=1), _tab("a", 11, window_id=1)]
    windows = {("a", 1): ("normal", "normal")}
    assert _plan(_mirror(tabs, [_inst("a")], windows), {"a"}) == []


def test_unready_instance_is_not_planned():
    tabs = [_tab("a", 10, window_id=1), _tab("a", 20, window_id=2)]
    windows = {("a", 1): ("normal", "normal"), ("a", 2): ("normal", "normal")}
    assert _plan(_mirror(tabs, [_inst("a")], windows), set()) == []  # not in ready_ids


# --- non-undoable classification (§9) ---------------------------------------
def test_window_merge_row_classified_non_undoable():
    # A journalled window_merge is reported un-undoable by undo._classify and never
    # counted toward reopens/closes (§9). Drop the branch => it silently vanishes.
    rows = [
        {"id": 1, "kind": "window_merge", "status": "done", "restored_at": None,
         "origin_action_id": None, "url": None},
    ]
    cls = _classify(rows)
    assert cls["impact"] == 0
    assert cls["un_undoable"] == [{"action_id": 1, "kind": "window_merge", "url": None}]


# --- integration: a real pass issues merge_windows, journals, keeps ages -----
def _settings(**over):
    s = dict(
        idle_minutes=60, pass_interval_min=5, cmd_timeout_ms=1500,
        snapshot_timeout_ms=1500, lease_ttl_ms=600_000, quarantine_ttl_min=1440,
        main_instance_id="main", max_actions_per_pass=20,
    )
    s.update(over)
    return SimpleNamespace(**s)


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


class _Ext:
    """Minimal emulator supporting per-instance multi-window snapshots."""

    def __init__(self, db):
        self.db = db
        self.registry = Registry()
        self.sessions: dict = {}
        self.tabs: dict = {}
        self.windows: dict = {}
        self.focused: dict = {}
        self.responder = None

    async def add_instance(self, iid, *, tabs, windows, focused, session="s"):
        cs = ConnState(ws=_FakeWS(), conn_epoch=1, install_uuid="u", session_id=session)
        self.registry.put(iid, cs)
        self.sessions[iid] = session
        self.tabs[iid] = tabs
        self.windows[iid] = windows
        self.focused[iid] = focused
        await self.db.write(lambda c: c.execute(
            "INSERT INTO instances (id, conn_epoch, connected, session_id, status) "
            "VALUES (?, 1, 1, ?, 'active')", (iid, session)))

    def _snapshot(self, iid, rid):
        return {
            "type": protocol.TYPE_SNAPSHOT, "id": rid, "sessionId": self.sessions[iid],
            "focusedWindowId": self.focused[iid], "tabs": self.tabs[iid],
            "windows": self.windows[iid],
        }

    async def _handle(self, iid, cs, frame):
        if frame.get("type") == protocol.TYPE_SNAPSHOT_REQUEST:
            await channel._handle_snapshot(self.db, self.registry, cs, iid, self._snapshot(iid, frame["id"]))
        elif frame.get("type") == protocol.TYPE_COMMAND:
            resp = self.responder(iid, frame["command"], frame["params"]) if self.responder else {"ok": True, "result": {}}
            resolve_response(cs, {"type": protocol.TYPE_RESPONSE, "id": frame["id"], **resp})

    async def run_pass(self, **kw):
        stop = asyncio.Event()

        async def driver():
            seen: dict = {}
            while not stop.is_set():
                for iid, cs in self.registry.items():
                    seen.setdefault(iid, 0)
                    while seen[iid] < len(cs.ws.sent):
                        frame = cs.ws.sent[seen[iid]]
                        seen[iid] += 1
                        await self._handle(iid, cs, frame)
                await asyncio.sleep(0.003)

        d = asyncio.create_task(driver())
        try:
            return await runner.run_pass(self.db, self.registry, _settings(), **kw)
        finally:
            stop.set()
            await d


def _tabinfo(tab_id, *, window_id, pinned=False, active=False, audible=False,
             age_ms=2 * HOUR, url="https://x/"):
    return {
        "tabId": tab_id, "windowId": window_id, "url": url, "title": "t",
        "favIconUrl": None, "pinned": pinned, "active": active, "audible": audible,
        "ageMs": age_ms, "openedAgoMs": age_ms, "ageUnknown": False, "selfNavigating": False,
    }


async def _mkdb(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def _rows(db, sql, params=()):
    return await db.read(lambda c: c.execute(sql, params).fetchall())


async def test_pass_issues_merge_command_journals_and_keeps_ages(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        ext = _Ext(db)
        # Two normal windows, all tabs idle 2h, none on-screen/audible. Window 1 has
        # two tabs (=> target), window 2 has one unpinned tab (=> source, folds in).
        # A thematic instance (main is exempt from auto-merge, §9); the end-to-end
        # merge mechanism is identical for thematic instances.
        await ext.add_instance(
            "media",
            tabs=[
                _tabinfo(10, window_id=1),
                _tabinfo(11, window_id=1),
                _tabinfo(20, window_id=2),
            ],
            windows=[
                {"id": 1, "type": "normal", "state": "normal"},
                {"id": 2, "type": "normal", "state": "normal"},
            ],
            focused=None,
        )
        seen = {}

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_MERGE_WINDOWS:
                seen["merge"] = params
                return {"ok": True, "result": {"merged": len(params.get("windowIds", []))}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"

        # The merge command was issued with the right window plan (target=1, source=[2]).
        assert seen["merge"] == {"windowIds": [2], "targetWindowId": 1}

        # A non-undoable window_merge row was journalled for the instance.
        wm = await _rows(db, "SELECT instance_from, status, initiator, detail "
                             "FROM actions WHERE kind='window_merge'")
        assert len(wm) == 1
        assert wm[0][0] == "media" and wm[0][1] == "done" and wm[0][2] == "curator"
        assert '"targetWindowId": 1' in wm[0][3] and '"moved_tab_ids": [20]' in wm[0][3]
        assert res["actions_count"] == 1

        # §17: the pass did NOT re-stamp the merged tabs' ages — every tab (the moved
        # one included) is STILL idle by well over IDLE_MINUTES. A server that
        # rejuvenated the merged window would push last_active_at to ~now (age ~= 0).
        after = await _rows(db, "SELECT tab_id, last_active_at FROM tabs ORDER BY tab_id")
        now_after = int(time.time() * 1000)
        assert after and all((now_after - la) >= IDLE for _tid, la in after)

        # §9: the journalled merge is honestly un-undoable in a pass rollback.
        def _read_rows(c):
            import sqlite3
            c.row_factory = sqlite3.Row
            return c.execute("SELECT * FROM actions WHERE pass_id=?", (res["pass_id"],)).fetchall()
        rows = await db.read(_read_rows)
        cls = _classify(rows)
        assert cls["impact"] == 0
        assert any(u["kind"] == "window_merge" for u in cls["un_undoable"])
    finally:
        await db.close()


async def test_merge_journal_excludes_tabs_closed_by_the_same_pass(tmp_path):
    """§9 wants the list of tabs that were actually MOVED.

    The merge plan is built from the mirror frozen BEFORE steps 4-8, so a tab that this
    very pass closed would otherwise be journalled as "moved" — a move that never
    happened, in a row the archive treats as the record of what the merge did. Here the
    thematic instance's tab 20 (in the source window) is dedupe-closed against prox, so
    only tab 21 is left to move. Reddens if the plan's raw ``moved_tab_ids`` is
    journalled: 20 reappears. (main itself is exempt from auto-merge, §9.)
    """
    db = await _mkdb(tmp_path)
    try:
        await db.write(lambda c: c.execute(
            "INSERT INTO rules (pattern, instance_id, singleton, invalid, created_at) "
            "VALUES ('grafana.lc', 'prox', 0, 0, 0)"))
        ext = _Ext(db)
        # media: window 1 = two unruled tabs (target), window 2 = the ruled tab 20 plus
        # an unruled 21 (source). Tie on tab count => target is the smaller id, 1.
        await ext.add_instance(
            "media",
            tabs=[
                _tabinfo(10, window_id=1, url="https://a/"),
                _tabinfo(11, window_id=1, url="https://b/"),
                _tabinfo(20, window_id=2, url="https://grafana.lc/d/x"),
                _tabinfo(21, window_id=2, url="https://c/"),
            ],
            windows=[
                {"id": 1, "type": "normal", "state": "normal"},
                {"id": 2, "type": "normal", "state": "normal"},
            ],
            focused=None,
        )
        # prox already holds the identical url => main:20 is a dedupe_close, not a move.
        await ext.add_instance(
            "prox",
            tabs=[_tabinfo(99, window_id=1, url="https://grafana.lc/d/x")],
            windows=[{"id": 1, "type": "normal", "state": "normal"}],
            focused=None,
        )

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                       "url": "https://grafana.lc/d/x"}}}
            if cmd == protocol.CMD_MERGE_WINDOWS:
                return {"ok": True, "result": {"merged": 1}}
            return {"ok": True, "result": {"ok": True}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # The dedupe really happened (so 20 is gone from the mirror).
        assert await _rows(db, "SELECT status FROM actions WHERE kind='dedupe_close'") == [("done",)]
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='media' AND tab_id=20") == [(0,)]
        # ... and the merge journal lists only the tab that could still move.
        detail = (await _rows(db, "SELECT detail FROM actions WHERE kind='window_merge'"))[0][0]
        assert '"moved_tab_ids": [21]' in detail
    finally:
        await db.close()
