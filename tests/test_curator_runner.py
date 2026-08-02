"""Integration tests for the curator pass runner (§7).

A tiny extension emulator drives real ``run_pass`` calls: it answers the pass's
``snapshot_request`` frames (via the real channel handler, so ``last_applied_snapshot_id``
is set exactly as in production) and scripts the ``open_tab`` / ``get_tab`` /
``close_tab`` command replies. Covers the required acceptance + coverage points:
concurrent-passes-one-runs, source-discard-completes, clock-jump-no-eviction,
foreign-snapshot-doesn't-eject, phase-B-source-mismatch (no un-quenchable loop),
non-convergence latch, and passes-row-on-empty.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.curator import runner
from src.db.access import Database
from src.ext import channel, protocol
from src.ext.commands import resolve_response
from src.ext.registry import ConnState, Registry

HOUR = 3_600_000


def _settings(**over):
    s = dict(
        idle_minutes=60,
        pass_interval_min=5,
        cmd_timeout_ms=1500,
        snapshot_timeout_ms=1500,
        lease_ttl_ms=600_000,
        quarantine_ttl_min=1440,
        main_instance_id="main",
    )
    s.update(over)
    return SimpleNamespace(**s)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


class Ext:
    """Extension emulator: one process worth of instances behind FakeWS sockets."""

    def __init__(self, db):
        self.db = db
        self.registry = Registry()
        self.sessions: dict = {}
        self.tabs: dict = {}          # instance_id -> list[TabInfo dict]
        self.focused: dict = {}       # instance_id -> focusedWindowId
        self.responder = None         # fn(instance_id, command, params) -> response dict
        self._open_seq = 1000

    async def add_instance(self, instance_id, session="s", tabs=None, focused=None, conn_epoch=1):
        cs = ConnState(ws=FakeWS(), conn_epoch=conn_epoch, install_uuid="u", session_id=session)
        self.registry.put(instance_id, cs)
        self.sessions[instance_id] = session
        self.tabs[instance_id] = list(tabs or [])
        self.focused[instance_id] = focused
        # Seed the instances row so apply_snapshot's epoch guard passes.
        await self.db.write(
            lambda c: c.execute(
                "INSERT INTO instances (id, conn_epoch, connected, session_id) "
                "VALUES (?, ?, 1, ?) ON CONFLICT(id) DO UPDATE SET "
                "conn_epoch=excluded.conn_epoch, connected=1, session_id=excluded.session_id",
                (instance_id, conn_epoch, session),
            )
        )
        return cs

    def _snapshot(self, instance_id, rid):
        return {
            "type": protocol.TYPE_SNAPSHOT,
            "id": rid,
            "sessionId": self.sessions[instance_id],
            "focusedWindowId": self.focused.get(instance_id),
            "tabs": self.tabs[instance_id],
            "windows": [{"id": 1, "type": "normal", "state": "normal"}],
        }

    def next_open_id(self):
        self._open_seq += 1
        return self._open_seq

    async def _handle_frame(self, instance_id, cs, frame):
        if frame.get("type") == protocol.TYPE_SNAPSHOT_REQUEST:
            snap = self._snapshot(instance_id, frame["id"])
            await channel._handle_snapshot(self.db, self.registry, cs, instance_id, snap)
        elif frame.get("type") == protocol.TYPE_COMMAND:
            resp = self.responder(instance_id, frame["command"], frame["params"]) if self.responder else {"ok": True, "result": {}}
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
                        await self._handle_frame(iid, cs, frame)
                await asyncio.sleep(0.003)

        d = asyncio.create_task(driver())
        try:
            return await runner.run_pass(self.db, self.registry, _settings(), **kw)
        finally:
            stop.set()
            await d


def _tabinfo(tab_id, url, *, window_id=1, pinned=False, active=False, audible=False,
             age_ms=2 * HOUR, opened_ago_ms=2 * HOUR, title="t", age_unknown=False):
    return {
        "tabId": tab_id, "windowId": window_id, "url": url, "title": title,
        "favIconUrl": None, "pinned": pinned, "active": active, "audible": audible,
        "ageMs": age_ms, "openedAgoMs": opened_ago_ms, "ageUnknown": age_unknown,
        "selfNavigating": False,
    }


async def _mkdb(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def _seed_rule(db, pattern, instance_id, *, singleton=0):
    await db.write(lambda c: c.execute(
        "INSERT INTO rules (pattern, instance_id, singleton, invalid, created_at) "
        "VALUES (?, ?, ?, 0, 0)", (pattern, instance_id, singleton)))


async def _seed_relocate(db, **kw):
    from src.db.actions import insert_action
    kw.setdefault("ts", 1)
    kw.setdefault("kind", "relocate")
    kw.setdefault("status", "done")
    kw.setdefault("initiator", "curator")
    return await db.write(lambda c: insert_action(c, **kw))


async def _rows(db, sql, params=()):
    return await db.read(lambda c: c.execute(sql, params).fetchall())


# --- phase A happy path ------------------------------------------------------
async def test_phase_a_opens_copy_and_writes_relocate(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_OPEN_TAB:
                return {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        rel = await _rows(db, "SELECT instance_from, instance_to, tab_id, status, decision "
                              "FROM actions WHERE kind='relocate' AND status='done'")
        assert rel == [("main", "prox", 20, "done", "rule_home")]
        # The source tab is NOT touched this pass (two-phase, §7).
        src = await _rows(db, "SELECT 1 FROM tabs WHERE instance_id='main' AND tab_id=20")
        assert src == [(1,)]
        # A copy row exists in prox with the inherited (idle) clock.
        copy = await _rows(db, "SELECT instance_id FROM tabs WHERE instance_id='prox'")
        assert len(copy) == 1
        # passes row recorded, one instance-considered relocation.
        p = await _rows(db, "SELECT ok, instances_ready, actions_count FROM passes")
        assert p == [(1, 2, 1)]
    finally:
        await db.close()


# --- passes row on an empty pass --------------------------------------------
async def test_passes_row_written_on_empty_pass(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        # A ready instance with only an unruled main tab => no decisions.
        await ext.add_instance("main", tabs=[_tabinfo(1, "https://random.example/p")])
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}
        res = await ext.run_pass()
        assert res["status"] == "ok"
        assert res["actions_count"] == 0
        # No actions, but a passes row EXISTS (else "ran empty" == "no passes").
        assert await _rows(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        p = await _rows(db, "SELECT ok, instances_ready, actions_count FROM passes")
        assert p == [(1, 1, 0)]
    finally:
        await db.close()


# --- phase B completes even after the source was DISCARDED (new tab_id) ------
async def test_source_discard_between_phases_completes(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        # The relocate row's source hint is tab_id=20, but the source was discarded
        # and now carries tab_id=77 with the SAME url (§5). Phase B must find it by
        # (instance, session, full URL), close 77, and complete.
        await ext.add_instance("main", tabs=[_tabinfo(77, "https://grafana.lc/d/abc")])
        await ext.add_instance("prox", tabs=[_tabinfo(99, "https://grafana.lc/d/abc")])
        await _seed_relocate(
            db, instance_from="main", instance_to="prox", tab_id=20,
            session_id_from="s", tab_id_to=99, session_id_to="s",
            url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc",
        )
        closed = {}

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"]}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                closed["tab"] = params["tabId"]
                return {"ok": True, "result": {"ok": True}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        assert closed["tab"] == 77  # closed by URL match, not the stale hint 20
        rc = await _rows(db, "SELECT status, tab_id FROM actions WHERE kind='relocate_close'")
        assert rc == [("done", 77)]
        # Source tab removed from the mirror.
        assert await _rows(db, "SELECT 1 FROM tabs WHERE instance_id='main' AND tab_id=77") == []
    finally:
        await db.close()


# --- phase B: source URL mismatch => abandoned, NO un-quenchable close loop --
async def test_phase_b_source_mismatch_abandons_no_loop(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        # Source now shows a DIFFERENT (unruled) url => the human moved it.
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://moved.example/here")])
        await ext.add_instance("prox", tabs=[])
        reloc_id = await _seed_relocate(
            db, instance_from="main", instance_to="prox", tab_id=20,
            session_id_from="s", tab_id_to=99, session_id_to="s",
            url="https://grafana.lc/d/OLD", url_norm="https://grafana.lc/d/OLD",
        )
        sent_cmds = []
        ext.responder = lambda i, c, p: (sent_cmds.append((c, p)) or {"ok": True, "result": {}})

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # The row is abandoned (drop the source-first check => it stays 'done' and
        # loops close_tab -> precondition_failed forever).
        st = await _rows(db, "SELECT status FROM actions WHERE id=?", (reloc_id,))
        assert st == [("abandoned",)]
        # No close_tab / get_tab was ever sent for this stale relocation.
        assert all(c != protocol.CMD_CLOSE_TAB for c, _ in sent_cmds)
        # And NO quarantine was written on the stale url_norm.
        assert await _rows(db, "SELECT COUNT(*) FROM quarantine") == [(0,)]
    finally:
        await db.close()


# --- phase B: source reconnects (epoch bump) MID-PASS => ejected, no strike --
async def test_phase_b_source_epoch_bump_midpass_ejects_no_strike(tmp_path):
    """WARNING 3 (§7): an instance whose ``conn_epoch`` changes between readiness
    capture and execution is ejected from the pass. Here the SOURCE reconnects while
    the target ``get_tab`` is in flight: the ``close_tab`` must be SKIPPED (deferred) —
    no ``precondition_failed`` strike, no quarantine, the relocate row stays live.

    Reddens if the pre-close readiness re-check is removed: the close would then be
    sent, the responder returns precondition_failed, and a strike row appears.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/abc")])
        await ext.add_instance("prox", tabs=[_tabinfo(99, "https://grafana.lc/d/abc")])
        reloc_id = await _seed_relocate(
            db, instance_from="main", instance_to="prox", tab_id=20,
            session_id_from="s", tab_id_to=99, session_id_to="s",
            url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc",
        )
        sent_cmds = []

        def respond(iid, cmd, params):
            sent_cmds.append((iid, cmd))
            if cmd == protocol.CMD_GET_TAB:
                # The target copy is alive — but the SOURCE reconnects RIGHT NOW
                # (mid-pass), bumping its conn_epoch past the captured readiness.
                ext.registry.get("main").conn_epoch += 1
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                        "url": "https://grafana.lc/d/abc"}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                # Only reached if the guard is gone: simulate the stale-tab_id reject.
                return {"ok": False, "error": {"code": protocol.ERR_PRECONDITION_FAILED}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # The source close was NEVER sent (ejected/deferred).
        assert all(not (iid == "main" and cmd == protocol.CMD_CLOSE_TAB) for iid, cmd in sent_cmds)
        # No strike row of any kind (a precondition strike would create one).
        assert await _rows(db, "SELECT COUNT(*) FROM quarantine") == [(0,)]
        # The relocate row is still live (not abandoned, not completed) — retries next pass.
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (reloc_id,)) == [("done",)]
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate_close'") == [(0,)]
    finally:
        await db.close()


# --- phase B non-vacuous: copy LIVE + source moved => abandon, no loop -------
async def test_phase_b_source_moved_copy_live_abandons_no_loop(tmp_path):
    """SUGGESTION 5 (§7): non-vacuous "no precondition_failed loop" case. The copy is
    LIVE in the target (so a target-first ordering would pass its ``get_tab`` and then
    close the source), AND the human moved the source to a new url. Source-first must
    abandon the row with NO ``close_tab``, NO ``precondition_failed`` strike, NO
    quarantine. The ``close_tab`` responder returns precondition_failed so that a
    target-first variant would visibly strike; source-first never sends it.
    """
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        # Source moved to a DIFFERENT (unruled) url; the copy is ALIVE in prox.
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://moved.example/here")])
        await ext.add_instance("prox", tabs=[_tabinfo(99, "https://grafana.lc/d/OLD")])
        reloc_id = await _seed_relocate(
            db, instance_from="main", instance_to="prox", tab_id=20,
            session_id_from="s", tab_id_to=99, session_id_to="s",
            url="https://grafana.lc/d/OLD", url_norm="https://grafana.lc/d/OLD",
        )
        sent_cmds = []

        def respond(iid, cmd, params):
            sent_cmds.append((iid, cmd))
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                        "url": "https://grafana.lc/d/OLD"}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                return {"ok": False, "error": {"code": protocol.ERR_PRECONDITION_FAILED}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # Row abandoned by the source-first mismatch check (decide), NOT closed.
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (reloc_id,)) == [("abandoned",)]
        # No close_tab was ever sent to the moved source => no loop.
        assert all(cmd != protocol.CMD_CLOSE_TAB for _iid, cmd in sent_cmds)
        # No precondition_failed strike on the stale url_norm.
        assert await _rows(db, "SELECT COUNT(*) FROM quarantine") == [(0,)]
    finally:
        await db.close()


# --- phase A: malformed open_tab reply (non-int tabId) => no rows written -----
async def test_phase_a_invalid_tab_id_writes_no_rows(tmp_path):
    """SUGGESTION 6 (§7): ``open_tab`` replies without an int ``tabId``. SQLite's
    INTEGER affinity would store a string as ``tab_id`` / ``tab_id_to`` — a live
    relocate row pointing at a non-existent copy. The guard must write NOTHING.

    Reddens if the validation is removed: a relocate row (and a copy tabs row + a
    non-convergence strike) would be written with the bogus string id.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])
        ext.responder = lambda i, c, p: (
            {"ok": True, "result": {"tabId": "not-an-int", "windowId": 1}}
            if c == protocol.CMD_OPEN_TAB else {"ok": True, "result": {}}
        )

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # No relocate row, no copy tab in prox, no non-convergence strike.
        assert await _rows(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='prox'") == [(0,)]
        assert await _rows(db, "SELECT COUNT(*) FROM quarantine") == [(0,)]
        assert res["actions_count"] == 0
    finally:
        await db.close()


# --- dedupe/singleton: survivor vanished before close => source NOT closed ----
async def test_close_survivor_vanished_source_not_closed(tmp_path):
    """SUGGESTION 4 (§7): an inter-instance ``dedupe_close`` closes the source only
    because a survivor holds the url in the target. If the human closed that survivor
    between the snapshot and now, closing the source would delete the LAST copy. The
    curator must ``get_tab`` the survivor first and, finding it gone, NOT close.

    Reddens if the survivor-verify is removed: the source close_tab is sent, the source
    tab is deleted, and a dedupe_close 'done' row appears.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        # main's tab is ruled to prox; prox already holds the identical url (frozen
        # mirror) => decide schedules a dedupe_close of the main tab, survivor in prox.
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[_tabinfo(99, "https://grafana.lc/d/x")])
        sent_cmds = []

        def respond(iid, cmd, params):
            sent_cmds.append((iid, cmd))
            if cmd == protocol.CMD_GET_TAB:
                # The survivor vanished since the snapshot.
                return {"ok": False, "error": {"code": protocol.ERR_NO_SUCH_TAB}}
            if cmd == protocol.CMD_CLOSE_TAB:
                return {"ok": True, "result": {"ok": True}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # No close_tab was sent to the source (survivor gone).
        assert all(cmd != protocol.CMD_CLOSE_TAB for _iid, cmd in sent_cmds)
        # The source tab is still present; no dedupe_close row was written.
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='main' AND tab_id=20") == [(1,)]
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='dedupe_close'") == [(0,)]
    finally:
        await db.close()


# --- lease renewal survives a transient write fault (does not stop the loop) --
async def test_renew_loop_survives_transient_error():
    """WARNING 1 (Фаза-8 sub-path): a single transient renewal write error must not
    stop the loop — a stopped loop lets the lease expire and another pass bump the
    epoch mid-flight. Reddens if the transient error returns instead of continuing.
    """
    calls = {"n": 0}

    class DB:
        async def write(self, fn):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient db fault")
            return True  # keep the lease

    # interval = ttl/3/1000 = 10ms; a few iterations fit in the sleep window.
    task = asyncio.create_task(runner._renew_loop(DB(), "owner", 1, ttl_ms=30))
    try:
        deadline = time.monotonic() + 1.0
        while calls["n"] < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        # Survived the first-call error AND kept renewing afterwards.
        assert calls["n"] >= 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_renew_loop_stops_on_lost_lease():
    """A definitive lost lease (``renew`` -> False) stops the loop on its own."""
    class DB:
        async def write(self, fn):
            return False  # lease lost

    await asyncio.wait_for(runner._renew_loop(DB(), "owner", 1, ttl_ms=30), timeout=1.0)


# --- two concurrent passes: exactly one runs (lease + fencing) --------------
async def test_two_concurrent_passes_one_runs(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])
        ext.responder = lambda i, c, p: (
            {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            if c == protocol.CMD_OPEN_TAB else {"ok": True, "result": {}}
        )

        # Fire two passes at once; the lease admits exactly one.
        r1, r2 = await asyncio.gather(ext.run_pass(), ext.run_pass())
        statuses = sorted([r1["status"], r2["status"]])
        assert statuses == ["lease_unavailable", "ok"]
        # Exactly one passes row, exactly one relocate action.
        assert await _rows(db, "SELECT COUNT(*) FROM passes") == [(1,)]
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate'") == [(1,)]
    finally:
        await db.close()


# --- server clock jump forward => abort, snapshot everyone, no eviction ------
async def test_clock_jump_forward_no_mass_eviction(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        # A fleet full of idle tabs that WOULD be relocated/closed on a normal pass.
        await ext.add_instance("main", tabs=[
            _tabinfo(1, "https://grafana.lc/d/a"),
            _tabinfo(2, "https://grafana.lc/d/b"),
            _tabinfo(3, "https://grafana.lc/d/c"),
        ])
        await ext.add_instance("prox", tabs=[])
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        # A clock guard that trips on its first check (wall jumped, monotonic did not).
        wall = iter([1000.0, 5000.0]); mono = iter([500.0, 505.0])
        from src.curator.clock import ClockGuard
        guard = ClockGuard(300.0, wall=lambda: next(wall), mono=lambda: next(mono))

        res = await ext.run_pass(clock_guard=guard)
        assert res["status"] == "clock_step"
        # NOTHING was evicted: no actions, no passes row (aborted before the lease).
        assert await _rows(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert await _rows(db, "SELECT COUNT(*) FROM passes") == [(0,)]
        # A snapshot_request went to EVERY connected instance (rebases the mirror).
        for iid, cs in ext.registry.items():
            assert any(f.get("type") == protocol.TYPE_SNAPSHOT_REQUEST for f in cs.ws.sent)
    finally:
        await db.close()


# --- readiness keys on the pass's OWN snapshot id, not the column ------------
async def test_await_ready_keys_on_own_request_id(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        reg = Registry()
        cs = ConnState(ws=FakeWS(), conn_epoch=1, install_uuid="u", session_id="s")
        reg.put("i1", cs)
        sent = await runner._request_all_snapshots(reg)
        _cs, rid = sent["i1"]

        # A FOREIGN snapshot (different id, e.g. a Cmd+T /api/state) was applied:
        # readiness must NOT count it — keying on the column instead of our id would.
        cs.last_applied_snapshot_id = "foreign-" + rid
        assert await runner._await_ready(reg, sent, timeout_ms=80) == {}

        # Our own request id lands => now ready.
        cs.last_applied_snapshot_id = rid
        ready = await runner._await_ready(reg, sent, timeout_ms=80)
        assert set(ready) == {"i1"}
    finally:
        await db.close()


# --- a foreign snapshot mid-pass does NOT eject the instance -----------------
async def test_foreign_snapshot_midpass_doesnt_eject(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_OPEN_TAB:
                # Simulate a human Cmd+T landing a FOREIGN snapshot on 'main' right
                # in the middle of the pass: it moves snapshot_at and last_applied to
                # a NEW id. A column-keyed pass would have dropped 'main' — an id-keyed
                # one already captured readiness, so the relocation still completes.
                cs = ext.registry.get("main")
                cs.last_applied_snapshot_id = "foreign-cmd-t"
                return {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # The relocation still happened despite the mid-pass foreign snapshot.
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate' AND status='done'") == [(1,)]
        assert res["instances_ready"] == 2
    finally:
        await db.close()


# --- non-convergence latch quarantines the source after K unproductive rounds -
async def test_non_convergence_latch_quarantines(tmp_path):
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/abc")])
        await ext.add_instance("prox", tabs=[])

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_OPEN_TAB:
                # Phase A opens, but the copy NEVER survives to the next snapshot
                # (prox stays empty) => phase B always finds it gone and abandons.
                return {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": False, "error": {"code": protocol.ERR_NO_SUCH_TAB}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        # Run passes until the source pair is quarantined (§7: three unproductive
        # phase-A opens without a completing phase B). A/B alternate, so it takes a
        # handful of passes; cap the loop generously.
        quarantined = None
        for _ in range(12):
            await ext.run_pass()
            rows = await _rows(db, "SELECT reason, until FROM quarantine "
                                   "WHERE instance_id='main' AND until > 0")
            if rows:
                quarantined = rows[0]
                break
        assert quarantined is not None, "source was never quarantined"
        assert quarantined[0] == "nonconvergent"

        # Once quarantined, phase A STOPS (the step-4 guard blocks the source).
        opens_before = await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate' AND status='done'")
        await ext.run_pass()
        opens_after = await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate' AND status='done'")
        assert opens_after == opens_before
    finally:
        await db.close()
