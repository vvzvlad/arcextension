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
import sqlite3
import time
from types import SimpleNamespace

import pytest

from src.curator import lease as lease_mod
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
        # Seed the instances row so apply_snapshot's epoch guard passes. status='active'
        # models an enrolled instance (issue #35): the known_instance_ids / send_command
        # status filters only see 'active' rows.
        await self.db.write(
            lambda c: c.execute(
                "INSERT INTO instances (id, conn_epoch, connected, session_id, status) "
                "VALUES (?, ?, 1, ?, 'active') ON CONFLICT(id) DO UPDATE SET "
                "conn_epoch=excluded.conn_epoch, connected=1, session_id=excluded.session_id, "
                "status='active'",
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

    async def with_driver(self, coro):
        """Await ``coro`` while the emulator answers every frame the pass sends.

        Split out of :meth:`run_pass` so a caller that reaches the runner through
        something OTHER than ``runner.run_pass`` — ``src.api.pause.resume_now``, the
        resume button — gets the same live fleet underneath it.
        """
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
            return await coro
        finally:
            stop.set()
            await d

    async def run_pass(self, **kw):
        return await self.with_driver(
            runner.run_pass(self.db, self.registry, _settings(), **kw)
        )

    def as_app(self, settings=None):
        """The ``app`` shape ``src.api.pause.resume_now`` reads (``app.state.*``)."""
        return SimpleNamespace(state=SimpleNamespace(
            db=self.db, ext_registry=self.registry,
            settings=settings if settings is not None else _settings(),
        ))


def _tabinfo(tab_id, url, *, window_id=1, pinned=False, active=False, audible=False,
             age_ms=2 * HOUR, opened_ago_ms=2 * HOUR, title="t", age_unknown=False):
    return {
        "tabId": tab_id, "windowId": window_id, "url": url, "title": title,
        "favIconUrl": None, "pinned": pinned, "active": active, "audible": audible,
        "ageMs": age_ms, "openedAgoMs": opened_ago_ms, "ageUnknown": age_unknown,
        "selfNavigating": False,
    }


async def _mkdb(tmp_path, *, continuity=True):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    if continuity:
        # Establish continuity so these tests exercise STEADY-STATE passes. Without a
        # stored fingerprint the very first pass over a populated DB is a §7 first-run
        # break (dry_run + resume_pending) — that policy has its own tests below.
        await _establish_continuity(db)
    return db


async def _establish_continuity(db, *, idle_minutes=60, main_instance_id="main"):
    from src.curator import clock as clockmod

    fp = await db.read(lambda c: clockmod.current_fingerprint(
        c, idle_minutes=idle_minutes, main_instance_id=main_instance_id))
    await db.write(lambda c: clockmod.store_fingerprint(c, fp))


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


# --- #45 regression: the curator's open_tab frame NEVER names a window --------
async def test_phase_a_open_tab_frame_carries_no_window_id(tmp_path):
    """#45 acceptance 10: window addressing is an MCP-only feature. The curator's own
    pass must issue the SAME ``open_tab`` frame as before — no ``windowId`` key — so the
    extension's deterministic §9 auto-select (and its vanished-window retry) is untouched.
    Slip a ``windowId`` into phase A's frame and this reddens."""
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])

        seen_open_params = []

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_OPEN_TAB:
                seen_open_params.append(params)
                return {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        assert seen_open_params, "phase A never issued an open_tab frame"
        assert all("windowId" not in p for p in seen_open_params)
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


# --- a COMPLETED relocation is retired, not re-marked abandoned next pass -----
async def test_completed_relocation_retired_from_live_relocations(tmp_path):
    # Once phase B writes a `relocate_close` for a relocation, the source is closed
    # and the relocation is complete. It must NOT re-enter live_relocations next
    # pass — else `decide` finds no source and wrongly marks the SUCCESS `abandoned`
    # (corrupting the §10 journal, inflating metrics, capturing a new same-url tab).
    from src.curator.mirror import load_mirror
    from src.db.actions import insert_action
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        await ext.add_instance("main")
        await ext.add_instance("prox")
        reloc_id = await _seed_relocate(
            db, instance_from="main", instance_to="prox", tab_id=20,
            session_id_from="s", tab_id_to=99, session_id_to="s",
            url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc",
        )
        # Before phase B: the relocation is live.
        m1 = await db.read(load_mirror)
        assert [r.id for r in m1.live_relocations] == [reloc_id]
        # Phase B completes: a relocate_close references the relocate row.
        await db.write(lambda c: insert_action(
            c, ts=2, kind="relocate_close", status="done", initiator="curator",
            origin_action_id=reloc_id, instance_from="main", instance_to="prox",
            url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc"))
        # Now RETIRED: excluded from live_relocations (drop the NOT EXISTS => reddens).
        m2 = await db.read(load_mirror)
        assert [r.id for r in m2.live_relocations] == []
    finally:
        await db.close()


# --- a FAILED phase-B close is a retry, NOT a retirement --------------------
async def test_failed_relocate_close_leaves_relocation_live(tmp_path):
    # `relocate_close status='failed'` (a transient precondition_failed — source
    # turned active/pinned/audible between snapshot and close) is a RETRY, not a
    # completion. The relocation must stay live so phase B retries by tab_id_to
    # (drift-resistant: get_tab by id). Retiring on a failed close would re-route
    # the source through decide and, on url drift, open a SECOND copy.
    from src.curator.mirror import load_mirror
    from src.db.actions import insert_action
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        await ext.add_instance("main")
        await ext.add_instance("prox")
        reloc_id = await _seed_relocate(
            db, instance_from="main", instance_to="prox", tab_id=20,
            session_id_from="s", tab_id_to=99, session_id_to="s",
            url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc",
        )
        await db.write(lambda c: insert_action(
            c, ts=2, kind="relocate_close", status="failed", initiator="curator",
            origin_action_id=reloc_id, instance_from="main", instance_to="prox",
            url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc"))
        # Retire ONLY on a done close => a failed close keeps the row live.
        m = await db.read(load_mirror)
        assert [r.id for r in m.live_relocations] == [reloc_id]
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


# --- WARNING-1: lease lost between a good close_tab and the completion write --
def _bump_lease_epoch_raw(db_path):
    """Increment the fencing epoch from OUTSIDE the pass (as a pause would), so the
    pass's next guarded write is fenced with LeaseLost — simulating the WARNING-1
    window between a successful browser close and its completion write."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
            "WHERE key = 'pass_lease_epoch'"
        )
        conn.commit()
    finally:
        conn.close()


def _expire_lease_raw(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("UPDATE settings SET value = '0' WHERE key = 'pass_lease_until'")
        conn.commit()
    finally:
        conn.close()


async def test_phase_b_lease_lost_before_completion_pending_survives_then_reconciled(tmp_path):
    """THE race (§7 WARNING-1). Phase B: the browser closes the source successfully,
    then a pause bumps the lease epoch BEFORE the completion write. The `relocate_close`
    was recorded `pending` UNDER the guard first, so it SURVIVES the fenced completion
    (neuter the pending write => there is no row and this reddens). The NEXT pass, whose
    mirror no longer holds the source, reconciles the pending → `done` — at-least-once
    journal, undoable-state consistent.
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

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                       "url": "https://grafana.lc/d/abc"}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                # Browser closed the source successfully; a pause bumps the epoch NOW,
                # so the completion write below is fenced (LeaseLost).
                _bump_lease_epoch_raw(db.db_path)
                return {"ok": True, "result": {"ok": True}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res1 = await ext.run_pass()
        assert res1["error"] == "lease_lost"  # pass stopped at the fenced completion
        # The pending relocate_close SURVIVES (no journal hole). Neuter the pending
        # write and this row is absent => the close is unrecorded => reddens.
        rc1 = await _rows(db, "SELECT status, tab_id, origin_action_id "
                              "FROM actions WHERE kind='relocate_close'")
        assert rc1 == [("pending", 20, reloc_id)]

        # --- second pass: the source really closed, so it is gone from the mirror ----
        ext.tabs["main"] = []
        _expire_lease_raw(db.db_path)  # let the next pass acquire (pass 1 left it held)

        # The reconcile grace (#48: read_pending_closes skips rows younger than one
        # cmd_timeout_ms, so a synchronous relocate_tab still awaiting its close is not
        # reconciled underneath it) means the pending row is only reconcilable once it is
        # older than cmd_timeout_ms. A real next pass is pass_interval_min (minutes) later
        # — here the two passes fire back-to-back, so advance the clock past the grace.
        res2 = await ext.run_pass(now=runner._now_ms() + 60_000)
        assert res2["status"] == "ok"
        # Reconcile flipped the pending → done: the journal hole is closed at-least-once.
        rc2 = await _rows(db, "SELECT status, tab_id FROM actions WHERE kind='relocate_close'")
        assert rc2 == [("done", 20)]
        # No SECOND close_tab was sent (phase B did not re-run — the relocation is retired).
        assert res2["actions_count"] == 1  # exactly the reconcile completion
    finally:
        await db.close()


async def test_reconcile_reverts_pending_when_source_still_present_no_double_close(tmp_path):
    """Reconcile-revert (§7 WARNING-1). A prior-pass `pending` close whose source is
    STILL present (the close never took effect — a connection-class failure) must be
    ABANDONED, and `decide` re-issues the close THIS pass. The source is closed exactly
    once. Reddens if reconcile marks it done (a phantom close) or does not abandon it.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        # main's tab is ruled to prox; prox already holds the identical url => decide
        # schedules a dedupe_close of the main tab (survivor in prox).
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[_tabinfo(99, "https://grafana.lc/d/x")])
        # A leftover pending dedupe_close from a PRIOR pass, source still present.
        pending_id = await _seed_relocate(
            db, kind="dedupe_close", status="pending", decision="dedupe",
            instance_from="main", tab_id=20, session_id_from="s",
            url="https://grafana.lc/d/x", url_norm="https://grafana.lc/d/x",
        )
        closes = []

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                       "url": "https://grafana.lc/d/x"}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                closes.append(params["tabId"])
                return {"ok": True, "result": {"ok": True}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # The stale pending row was ABANDONED, not completed.
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (pending_id,)) == [("abandoned",)]
        # decide re-issued the close => exactly ONE done dedupe_close and ONE close_tab.
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='dedupe_close' AND status='done'") == [(1,)]
        assert closes == [20]
        # Source closed exactly once (tab removed from the mirror).
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='main' AND tab_id=20") == [(0,)]
    finally:
        await db.close()


async def test_reconcile_after_reconnect_url_present_abandons_not_phantom_done(tmp_path):
    """Finding #1: a pending close whose browser close NEVER happened (connection-class)
    then the source instance RECONNECTS with a new session — the tab persisted, same
    url. `_source_present` must key on URL (not session): the source is still open, so
    the pending is ABANDONED (and re-closed), NOT phantom-marked `done`. Reddens under
    the old session short-circuit (session mismatch => absent => done)."""
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        # Instance reconnected: NEW session "s2", but the same url is still open (a WS
        # reconnect does not close browser tabs). prox holds the survivor for the dedupe.
        await ext.add_instance("main", session="s2",
                               tabs=[_tabinfo(31, "https://grafana.lc/d/y")])
        await ext.add_instance("prox", session="s2",
                               tabs=[_tabinfo(99, "https://grafana.lc/d/y")])
        # A leftover pending dedupe_close from a PRIOR pass under the OLD session "s1".
        pending_id = await _seed_relocate(
            db, kind="dedupe_close", status="pending", decision="dedupe",
            instance_from="main", tab_id=20, session_id_from="s1",
            url="https://grafana.lc/d/y", url_norm="https://grafana.lc/d/y",
        )
        closes = []

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                       "url": "https://grafana.lc/d/y"}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                closes.append(params["tabId"])
                return {"ok": True, "result": {"ok": True}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # ABANDONED, not a phantom `done` (the source was still open across the reconnect).
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (pending_id,)) == [("abandoned",)]
        # And decide re-issued the close against the live (new-session) source tab 31.
        assert closes == [31]
    finally:
        await db.close()


async def test_reconcile_completes_pending_when_source_gone(tmp_path):
    """Reconcile-complete: a prior-pass `pending` dedupe_close whose source is GONE from
    the mirror (the close happened, just wasn't journaled) is flipped to `done`
    (at-least-once) and the pair's strikes are reset. No new close is issued."""
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])  # source already gone
        pending_id = await _seed_relocate(
            db, kind="dedupe_close", status="pending", decision="dedupe",
            instance_from="main", tab_id=20, session_id_from="s",
            url="https://grafana.lc/d/x", url_norm="https://grafana.lc/d/x",
        )
        # A leftover strike on the pair; a success (the completed close) resets it.
        await db.write(lambda c: c.execute(
            "INSERT INTO quarantine (instance_id, url, strikes, until, reason) "
            "VALUES ('main', 'https://grafana.lc/d/x', 2, 0, 'x')"))
        closes = []
        ext.responder = lambda i, c, p: (
            closes.append(p.get("tabId")) or {"ok": True, "result": {}}
        )

        res = await ext.run_pass()
        assert res["status"] == "ok"
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (pending_id,)) == [("done",)]
        assert closes == []  # nothing to close — the source was already gone
        # Strikes reset by the completion (success on the pair, §7).
        assert await _rows(db, "SELECT strikes FROM quarantine "
                               "WHERE instance_id='main' AND url='https://grafana.lc/d/x'") == [(0,)]
    finally:
        await db.close()


async def test_close_precondition_failed_updates_pending_to_failed_and_strikes(tmp_path):
    """precondition_failed still strikes (unchanged behaviour, now via the pending row).
    The source turned active/pinned between snapshot and close: the pending close row is
    updated → `failed` and one quarantine strike lands. Reddens if the strike is dropped.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[_tabinfo(99, "https://grafana.lc/d/x")])

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                       "url": "https://grafana.lc/d/x"}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                return {"ok": False, "error": {"code": protocol.ERR_PRECONDITION_FAILED}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # Exactly one dedupe_close row, and it is FAILED (not left pending, not done).
        assert await _rows(db, "SELECT status, reason FROM actions WHERE kind='dedupe_close'") == \
            [("failed", protocol.ERR_PRECONDITION_FAILED)]
        # A strike landed on the (source, url_norm) pair (drop _strike_once => reddens).
        assert await _rows(db, "SELECT strikes FROM quarantine "
                               "WHERE instance_id='main' AND url='https://grafana.lc/d/x'") == [(1,)]
        # Source NOT closed (precondition failed).
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='main' AND tab_id=20") == [(1,)]
    finally:
        await db.close()


async def test_dedupe_close_happy_path_one_done_row_tab_deleted(tmp_path):
    """Happy path unchanged: a normal inter-instance dedupe_close ends with exactly one
    `done` row (never a lingering `pending`), the source tab deleted, strikes reset."""
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[_tabinfo(99, "https://grafana.lc/d/x")])
        # Pre-existing strikes on the pair — a successful close resets them (§7).
        await db.write(lambda c: c.execute(
            "INSERT INTO quarantine (instance_id, url, strikes, until, reason) "
            "VALUES ('main', 'https://grafana.lc/d/x', 2, 0, 'x')"))

        def respond(iid, cmd, params):
            if cmd == protocol.CMD_GET_TAB:
                return {"ok": True, "result": {"tab": {"id": params["tabId"],
                                                       "url": "https://grafana.lc/d/x"}}}
            if cmd == protocol.CMD_CLOSE_TAB:
                return {"ok": True, "result": {"ok": True}}
            return {"ok": True, "result": {}}
        ext.responder = respond

        res = await ext.run_pass()
        assert res["status"] == "ok"
        # Exactly one dedupe_close and it is done — no pending left behind.
        assert await _rows(db, "SELECT status FROM actions WHERE kind='dedupe_close'") == [("done",)]
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='main' AND tab_id=20") == [(0,)]
        assert await _rows(db, "SELECT strikes FROM quarantine "
                               "WHERE instance_id='main' AND url='https://grafana.lc/d/x'") == [(0,)]
    finally:
        await db.close()


# --- reconcile is GATED on the source instance's readiness for THIS pass -----
async def test_reconcile_skipped_when_source_instance_not_ready(tmp_path):
    """BLOCKER: a pending close must NOT be resolved off a STALE mirror.

    The source instance does not answer this pass's snapshot_request (laptop asleep, the
    service worker never woke), so the mirror still shows the world as it was BEFORE the
    close. Without the readiness gate ``_source_present`` says "still there" and a close
    that really happened is terminally journalled ``abandoned`` — for a relocate_close
    that is permanent (``abandoned`` does not retire the relocation, so a completed
    relocation looks abandoned forever). The row must stay ``pending`` until a pass with
    a fresh snapshot. Reddens if the gate is removed: the status flips to 'abandoned'.
    """
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        # 'main' is registered but MUTE: it never answers the snapshot_request, so it is
        # never in ready_ids. Its mirror rows are the stale pre-close picture.
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await db.write(lambda c: c.execute(
            "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, pinned, "
            "active, opened_at, last_active_at, age_unknown, self_navigating, audible, "
            "updated_at) VALUES ('main', 20, 1, 'https://grafana.lc/d/x', 't', 0, 0, "
            "0, 0, 0, 0, 0, 0)"))
        pending_id = await _seed_relocate(
            db, kind="dedupe_close", status="pending", decision="dedupe",
            instance_from="main", tab_id=20, session_id_from="s",
            url="https://grafana.lc/d/x", url_norm="https://grafana.lc/d/x",
        )

        # Drop every frame on the floor => no snapshot is applied => 'main' is not ready.
        async def _mute(instance_id, cs, frame):
            return None
        ext._handle_frame = _mute

        res = await ext.run_pass()
        assert res["instances_ready"] == 0
        # STILL pending: neither done nor abandoned.
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (pending_id,)) == [("pending",)]
    finally:
        await db.close()


# --- revoke: retire-relocations pass step + drain cascade (issue #35 §5, acc 8) --
async def test_revoke_retires_relocation_invalidates_rule_and_keeps_drain(tmp_path):
    """Acceptance 8, end to end. Revoke a NON-main instance that owns a live relocation
    and a rule; ONE pass must:

      (a) mark the live relocation ``abandoned`` (the retire step) — remove the step and
          the row stays ``done`` => reddens;
      (b) mark the rule targeting the revoked instance ``invalid=1`` (the known_instance_ids
          filter) — revert the filter and the revoked id leaks back in, rule stays valid
          => reddens;
      (c) keep the ``X -> main`` rule VALID and the drain ON — MAIN is exempt in
          ``_revalidate_rules`` even with NO active main row here, so ``has_active_rules``
          stays True. Drop the exemption and both rules go invalid, the drain switches off
          => reddens.
    """
    from src.db.queries import revoke_instance
    from src.rules import access as rules_access
    from src.rules.preview import has_active_rules

    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        await ext.add_instance("prox", tabs=[])  # the ready survivor; answers snapshots
        # 'gone' is enrolled (active) but has NO live socket — it is not registered, so it
        # does not answer this pass's snapshot. main deliberately has NO instances row, so
        # the X->main rule's validity rests entirely on the main exemption.
        await db.write(lambda c: c.execute(
            "INSERT INTO instances (id, status, session_id, connected) "
            "VALUES ('gone', 'active', 's', 1)"))
        await _seed_rule(db, "gone.lc", "gone")   # target = the instance we revoke
        await _seed_rule(db, "keep.lc", "main")   # X -> main keeps the policy non-empty
        reloc_id = await _seed_relocate(
            db, instance_from="gone", instance_to="prox", tab_id=20,
            session_id_from="s", tab_id_to=99, session_id_to="s",
            url="https://gone.lc/x", url_norm="https://gone.lc/x",
        )
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        # Revoke 'gone' (non-main) — the transaction Task E will call.
        rev = await db.write(
            lambda c: revoke_instance(c, "gone", now=5000, main_instance_id="main")
        )
        assert rev.revoked and not rev.was_main
        assert await _rows(
            db, "SELECT status, session_id, revoked_at FROM instances WHERE id='gone'"
        ) == [("revoked", None, 5000)]

        res = await ext.run_pass()
        assert res["status"] == "ok"

        # (a) the live relocation was retired to 'abandoned' by the pass step.
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (reloc_id,)) == [("abandoned",)]
        # (b) the rule targeting the revoked instance is now invalid=1.
        assert await _rows(db, "SELECT invalid FROM rules WHERE instance_id='gone'") == [(1,)]
        # (c) the X->main rule stayed valid and the drain is still ON.
        assert await _rows(db, "SELECT invalid FROM rules WHERE instance_id='main'") == [(0,)]
        assert has_active_rules(await db.read(rules_access.list_rules)) is True
    finally:
        await db.close()


async def test_reconcile_runs_when_source_instance_IS_ready(tmp_path):
    """The control arm of the gate: same leftover pending row, but the source DOES
    answer this pass's snapshot and its tab is gone => reconcile completes it `done`."""
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])  # answers the snapshot; source gone
        pending_id = await _seed_relocate(
            db, kind="dedupe_close", status="pending", decision="dedupe",
            instance_from="main", tab_id=20, session_id_from="s",
            url="https://grafana.lc/d/x", url_norm="https://grafana.lc/d/x",
        )
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        res = await ext.run_pass()
        assert res["instances_ready"] == 1
        assert await _rows(db, "SELECT status FROM actions WHERE id=?", (pending_id,)) == [("done",)]
    finally:
        await db.close()


# --- first-run policy: a clean DB with a fleet defers behind a click (§7) ----
async def test_clean_db_with_populated_fleet_defers_to_resume_pending(tmp_path):
    """§7 lists "чистая БД" among the continuity breaks: every tab arrives with
    age_unknown=1 and last_active_at=now, so an hour later the WHOLE fleet turns
    eligible at once and one unconfirmed pass would drain it. With no stored
    fingerprint and a populated fleet the first pass must be a dry_run that arms
    resume_pending. Reddens if is_continuity_break keeps answering False on a missing
    fingerprint: the relocation would be executed unconfirmed."""
    db = await _mkdb(tmp_path, continuity=False)  # NO fingerprint: a genuinely fresh DB
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])
        ext.responder = lambda i, c, p: (
            {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            if c == protocol.CMD_OPEN_TAB else {"ok": True, "result": {}}
        )

        res = await ext.run_pass()
        assert res["status"] == "resume_pending"
        assert res["plan"]["relocations"] == 1     # the plan is shown, not executed
        # A dry_run writes nothing: no actions, no passes row, source untouched.
        assert await _rows(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert await _rows(db, "SELECT COUNT(*) FROM passes") == [(0,)]
        # The latch is armed and persists into the next scheduled pass.
        from src.curator.pause import RESUME_PENDING_KEY
        from src.db.settings_store import get_setting
        assert await db.read(lambda c: get_setting(c, RESUME_PENDING_KEY))
        assert (await ext.run_pass())["status"] == "resume_pending"

        # The confirming click runs the real pass and stores the fingerprint.
        res3 = await ext.run_pass(confirm_pending=True)
        assert res3["status"] == "ok"
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate'") == [(1,)]
        assert not await db.read(lambda c: get_setting(c, RESUME_PENDING_KEY))
    finally:
        await db.close()


async def test_clean_db_with_empty_fleet_runs_normally(tmp_path):
    """The other side of the boundary: a brand-new install whose browser has never
    connected has nothing to drain, so it must NOT sit waiting for a click."""
    db = await _mkdb(tmp_path, continuity=False)
    try:
        ext = Ext(db)  # no instances at all
        res = await ext.run_pass()
        assert res["status"] == "no_ready_instances"  # a REAL pass, not resume_pending
        assert await _rows(db, "SELECT COUNT(*) FROM passes") == [(1,)]
        # And it did NOT consume the first-run latch: no fingerprint was stored, so the
        # first pass that actually meets a fleet still defers behind the click.
        from src.curator import clock as clockmod
        assert await db.read(clockmod.read_stored_fingerprint) is None
    finally:
        await db.close()


# --- the resume BUTTON is the way out of a continuity-break latch (§7) ------
async def test_resume_now_escapes_a_continuity_break_latch_and_executes(tmp_path):
    """The exit from ``resume_pending`` armed by a CONTINUITY BREAK, end to end.

    The latch is armed by two events: an expired pause and a continuity break. For the
    pause there is something to clear, so a plain resume works. For a break there is
    NOT: no ``pause_until``, no ``pause_started_at`` — only the stale fingerprint, which
    is refreshed by a REAL pass and by nothing else. An unconfirmed pass returns
    ``{"status": "resume_pending"}`` at step 1 before doing anything, so the pass that
    would lift the latch is exactly the pass the latch blocks. Every press of the button
    cleared nothing, ran nothing and left the latch armed — the state had no exit
    through the UI at all.

    So this asserts the whole way out, not just a status string: the pass genuinely
    EXECUTES (the deferred relocation lands in ``actions``), the latch is gone, the
    stored fingerprint now matches the running config, and the NEXT pass is an ordinary
    one. Reddens if ``resume_now`` stops confirming the latch.
    """
    from src.api.pause import resume_now
    from src.curator import clock as clockmod
    from src.curator.pause import RESUME_PENDING_KEY
    from src.db.settings_store import get_setting

    db = await _mkdb(tmp_path, continuity=False)
    try:
        # Continuity was established at IDLE_MINUTES=30 but the service now runs with
        # 60 (`_settings()`): a §7 break, and one with no pause anywhere near it.
        await _establish_continuity(db, idle_minutes=30)
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])
        ext.responder = lambda i, c, p: (
            {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            if c == protocol.CMD_OPEN_TAB else {"ok": True, "result": {}}
        )

        # The break arms the latch: the plan is shown, nothing is executed.
        armed = await ext.run_pass()
        assert armed["status"] == "resume_pending"
        assert armed["plan"]["relocations"] == 1
        assert await _rows(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert await db.read(lambda c: get_setting(c, RESUME_PENDING_KEY))

        # The button. DELETE /api/pause and the MCP `resume` tool both come through here.
        out = await ext.with_driver(resume_now(ext.as_app()))

        # A REAL pass ran — not another `resume_pending` deferral…
        assert out["pass"]["status"] == "ok", out["pass"]
        # …and it actually did the work the plan promised.
        assert await _rows(
            db, "SELECT instance_from, instance_to, status FROM actions WHERE kind='relocate'"
        ) == [("main", "prox", "done")]
        assert await _rows(db, "SELECT COUNT(*) FROM passes") == [(1,)]

        # The latch is gone and the fingerprint was refreshed to the RUNNING config —
        # the two halves of "the state has an exit". Without the refresh the next tick
        # would re-arm on the same stale fingerprint and the loop would never end.
        assert not await db.read(lambda c: get_setting(c, RESUME_PENDING_KEY))
        stored = await db.read(clockmod.read_stored_fingerprint)
        assert stored["idle_minutes"] == 60

        # And the next scheduled pass is an ordinary one.
        assert (await ext.run_pass())["status"] == "ok"
    finally:
        await db.close()


# --- confirm_pending with NOTHING armed must not bypass the gates (§7) ------
async def test_confirm_pending_without_armed_latch_falls_back_to_the_gate(tmp_path):
    """A client that sends confirm_pending out of habit must not eat a continuity
    break: with no armed latch the flag is ignored and the pass goes through the normal
    step-1 gate, which sees the fingerprint mismatch (a lowered IDLE_MINUTES, §7) and
    arms resume_pending instead of draining the fleet. Reddens if confirm_pending is
    honoured unconditionally: the relocation runs and _finish_continuity silently
    rewrites the fingerprint."""
    db = await _mkdb(tmp_path)  # continuity established at IDLE_MINUTES=60
    try:
        await _seed_rule(db, "grafana.lc", "prox")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        await ext.add_instance("prox", tabs=[])
        ext.responder = lambda i, c, p: (
            {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            if c == protocol.CMD_OPEN_TAB else {"ok": True, "result": {}}
        )
        from src.curator.pause import RESUME_PENDING_KEY
        from src.db.settings_store import get_setting
        assert not await db.read(lambda c: get_setting(c, RESUME_PENDING_KEY))

        # IDLE_MINUTES drops 60 -> 30: a §7 continuity break, latch not yet armed.
        stop = asyncio.Event()

        async def driver():
            seen: dict = {}
            while not stop.is_set():
                for iid, cs in ext.registry.items():
                    seen.setdefault(iid, 0)
                    while seen[iid] < len(cs.ws.sent):
                        frame = cs.ws.sent[seen[iid]]
                        seen[iid] += 1
                        await ext._handle_frame(iid, cs, frame)
                await asyncio.sleep(0.003)

        d = asyncio.create_task(driver())
        try:
            res = await runner.run_pass(
                db, ext.registry, _settings(idle_minutes=30), confirm_pending=True
            )
        finally:
            stop.set()
            await d

        assert res["status"] == "resume_pending"
        assert await _rows(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert await db.read(lambda c: get_setting(c, RESUME_PENDING_KEY))
    finally:
        await db.close()


# --- the restore marker: read once, lazily, and its blindness is observable --
async def test_marker_read_once_per_pass_and_never_before_the_pause_gate(tmp_path):
    """Two properties of the marker read, both load-bearing.

    ONCE per pass: the step-1 comparison and the closing ``_finish_continuity`` must use
    the SAME value, or a marker rewritten between them is stored as if it had always been
    there and the break is swallowed for good.

    NOT AT ALL for a pass that exits at the pause gate: that path is triggered by every
    POST /api/run_pass and every pause/resume, and each touch of a wedged mount parks a
    thread for the read timeout. Reddens if the read moves back to pass entry: the paused
    pass bumps the counter.
    """
    from src.curator import clock as clockmod
    from src.curator import pause as pause_ops

    marker = tmp_path / "continuity-marker"
    marker.write_text("uuid-1")
    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}
        settings = _settings(restore_marker_path=str(marker))
        calls = {"n": 0}
        real = clockmod.read_restore_marker_async

        async def counting(path, **kw):
            calls["n"] += 1
            return await real(path, **kw)
        clockmod.read_restore_marker_async = counting
        try:
            await _drive(ext, db, settings)
            assert calls["n"] == 1        # one read served both consumers

            # A paused pass must not touch the filesystem at all.
            now = runner._now_ms()
            await db.write(lambda c: pause_ops.pause(c, now=now, minutes=30))
            res = await runner.run_pass(db, ext.registry, settings, now=now + 1_000)
            assert res["status"] == "paused"
            assert calls["n"] == 1        # unchanged
        finally:
            clockmod.read_restore_marker_async = real
    finally:
        await db.close()


async def test_unreadable_marker_is_published_for_metrics(tmp_path):
    """§12: "the restore detector is blind" must be scrapable STATE, not a log line.

    A configured marker the service can never read (the classic case: written as root
    with umask 077 while the container runs as uid 1000) disables restore detection
    permanently and silently — no other fingerprint component can catch a restore,
    they all travel inside the backup. The runner records it in ``settings`` so
    /metrics can export a 0/1 gauge at scrape time. Reddens if the row is not written.
    """
    from src.db.settings_store import get_setting

    db = await _mkdb(tmp_path)
    try:
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        # A path that cannot be read (a directory: open() raises IsADirectoryError).
        blind = tmp_path / "not-a-file"
        blind.mkdir()
        await _drive(ext, db, _settings(restore_marker_path=str(blind)))
        assert await db.read(
            lambda c: get_setting(c, runner._MARKER_UNREADABLE_KEY)) == "1"

        # A readable marker clears it again.
        good = tmp_path / "continuity-marker"
        good.write_text("uuid-1")
        await _drive(ext, db, _settings(restore_marker_path=str(good)))
        assert await db.read(
            lambda c: get_setting(c, runner._MARKER_UNREADABLE_KEY)) == "0"
    finally:
        await db.close()


async def _drive(ext, db, settings):
    """Run one pass with the frame driver, using a caller-supplied settings object."""
    stop = asyncio.Event()

    async def driver():
        seen: dict = {}
        while not stop.is_set():
            for iid, cs in ext.registry.items():
                seen.setdefault(iid, 0)
                while seen[iid] < len(cs.ws.sent):
                    frame = cs.ws.sent[seen[iid]]
                    seen[iid] += 1
                    await ext._handle_frame(iid, cs, frame)
            await asyncio.sleep(0.003)

    d = asyncio.create_task(driver())
    try:
        return await runner.run_pass(db, ext.registry, settings)
    finally:
        stop.set()
        await d


# --- a pause mid-command must NOT let a second pass in (MAJOR: no overlap) --
async def test_pause_during_phase_a_command_does_not_admit_a_second_pass(tmp_path):
    """Two passes must never run at once — the lease SLOT is what guarantees it.

    ``run_phase_a`` sends ``open_tab`` BEFORE its first guarded write, so a pause landing
    mid-command fences the pass only AFTER the tab exists in the browser while its
    ``tabs``/``relocate`` rows are rolled back. If the pause also freed the lease slot,
    the pass that §7 runs immediately on resume would start right then, decide against a
    mirror with no copy in it, and open a SECOND one — permanently, for a ``main`` target
    (a sink without dedup, §15). So: while the fenced pass is inside its command, the
    slot stays taken and a concurrent pass gets `lease_unavailable`; once it finishes, it
    releases and the next pass acquires at once.

    Reddens if ``pause()`` frees the slot: the inner pass returns ok/no_ready_instances.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "main")   # target = the un-dedupable sink
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])
        await ext.add_instance("prox", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        inner: dict = {}
        orig_handle = ext._handle_frame

        async def handle(iid, cs, frame):
            if (frame.get("type") == protocol.TYPE_COMMAND
                    and frame["command"] == protocol.CMD_OPEN_TAB
                    and "res" not in inner):
                # The outer pass is parked inside send_command awaiting this response.
                # Right here the owner hits the emergency stop and lifts it again — §7
                # makes the lift run a pass IMMEDIATELY. It must not start on top of the
                # pass that is, at this instant, still talking to a browser.
                inner["res"] = await _pause_then_resume_pass(db, ext)
                resolve_response(cs, {
                    "type": protocol.TYPE_RESPONSE, "id": frame["id"], "ok": True,
                    "result": {"tabId": ext.next_open_id(), "windowId": 1},
                })
                return
            await orig_handle(iid, cs, frame)
        ext._handle_frame = handle
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        outer = await ext.run_pass()
        ext._handle_frame = orig_handle

        # The concurrent pass was refused by the still-held slot.
        assert inner["res"]["status"] == "lease_unavailable"
        # The fenced pass stopped and wrote no relocate row for the copy it opened.
        assert outer["error"] == "lease_lost"
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate' AND status='done'") == [(0,)]
        # And the slot was handed back, so the NEXT pass starts without a TTL wait.
        assert (await db.read(lease_mod.read_lease))["until"] == 0
        assert (await ext.run_pass())["status"] == "ok"
    finally:
        await db.close()


async def _pause_then_resume_pass(db, ext):
    """Arm a pause (fencing the in-flight pass), lift it, and try a pass at once."""
    from src.curator import pause as pause_ops
    now = runner._now_ms()
    await db.write(lambda c: pause_ops.pause(c, now=now, minutes=30))
    await db.write(lambda c: pause_ops.resume(c, now=now + 1_000))
    return await runner.run_pass(db, ext.registry, _settings())


# --- §12: the pass re-validates the rules on EVERY pass ---------------------
async def test_pass_revalidates_rules_orphan_then_restored(tmp_path):
    """§12 "rules.instance_id валидируется ... на каждом проходе": a rule pointing at an
    instance that no longer exists must end up invalid=1 (it feeds curator_rules_invalid
    and the editor highlight), and must clear once the instance is back. Reddens if
    revalidate_rules is never called from the pass — the flag stays 0 forever."""
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "ghost")  # 'ghost' never existed
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        assert await ext.run_pass() is not None
        assert await _rows(db, "SELECT invalid FROM rules") == [(1,)]

        # The instance comes back => the pass clears the flag again.
        await ext.add_instance("ghost", tabs=[])
        await ext.run_pass()
        assert await _rows(db, "SELECT invalid FROM rules") == [(0,)]
    finally:
        await db.close()


async def test_revalidation_never_invalidates_a_rule_pointing_at_main(tmp_path):
    """§12 asks for ONE rule at save time and on every pass. The save-time check
    (``rules._validate_instance_or_422``) explicitly allows ``MAIN_INSTANCE_ID`` even
    with no ``instances`` row — a main that never connected is a modelled state
    (``curator_main_instance_never_seen``). Without the same exemption the pass
    invalidates a legally saved "X -> main" rule; being the only rule, the policy then
    reads as EMPTY, the unruled drain switches off and the curator silently stops doing
    anything at all. Reddens if the exemption is dropped: invalid flips to 1.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "main")   # main has never connected
        ext = Ext(db)
        await ext.add_instance("prox", tabs=[])      # only a themed instance is up
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        await ext.run_pass()
        assert await _rows(db, "SELECT invalid FROM rules") == [(0,)]
    finally:
        await db.close()


async def test_revalidation_runs_for_a_dry_run_so_the_confirmed_plan_matches(tmp_path):
    """The plan the owner confirms after a break must be computed with the SAME rule
    flags the confirming pass will use. Skipping revalidation on a dry_run made the plan
    route on stale flags while the executing pass revalidated first — "confirmed" then
    did not mean "this is what will happen". Reddens if revalidation is gated on
    ``not effective_dry_run``: invalid stays 0 after the dry run.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "ghost")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])
        ext.responder = lambda i, c, p: {"ok": True, "result": {}}

        res = await ext.run_pass(dry_run=True)
        assert res["status"] == "dry_run"
        assert await _rows(db, "SELECT invalid FROM rules") == [(1,)]
        # The dry_run still writes no journal: §12's promise is about actions/passes.
        assert await _rows(db, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert await _rows(db, "SELECT COUNT(*) FROM passes") == [(0,)]
    finally:
        await db.close()


# --- an orphaned (invalid) rule means "home unreachable", never "unruled" ----
async def test_orphaned_rule_defers_instead_of_draining_the_tab_into_main(tmp_path):
    """§12 asks for the orphaned rule to be FLAGGED (for the alert). It must not also
    change where the owner's tabs live.

    ``compile_rules`` drops invalid rules from matching, so a tab whose rule was just
    orphaned would stop being ruled and fall into the unruled -> ``main`` drain: the tab
    physically moves. And it would flip mid-life — ``insert_rule`` writes ``invalid=0``,
    so the rule behaves for one pass and starts relocating on the next. The behaviour
    must be identical on both sides of the flag: an unreachable target was ``deferred``
    before it was flagged, and stays ``deferred`` after.

    Reddens if the orphan-match is removed from ``_route``: a phase-A open into main
    appears and the tab leaves prox.

    The second, VALID rule is load-bearing scaffolding, not decoration: with only the
    orphaned rule in the table ``has_active_rules`` reports an empty policy once it is
    flagged, the unruled drain switches off, and the tab would stay in prox with or
    without the fix — the test would pass vacuously.
    """
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "ghost")   # target will be flagged invalid
        await _seed_rule(db, "other.lc", "prox")      # keeps the unruled drain ON
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])
        await ext.add_instance("prox", tabs=[_tabinfo(20, "https://grafana.lc/d/x")])
        ext.responder = lambda i, c, p: (
            {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            if c == protocol.CMD_OPEN_TAB else {"ok": True, "result": {}}
        )

        res = await ext.run_pass()
        assert res["status"] == "ok"
        assert await _rows(
            db, "SELECT instance_id, invalid FROM rules ORDER BY id"
        ) == [("ghost", 1), ("prox", 0)]   # only the orphan is flagged; drain stays on
        # NOT drained into main: no open_tab, no relocate, the tab is still in prox.
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate' AND status='done'") == [(0,)]
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='prox' AND tab_id=20") == [(1,)]
        assert await _rows(db, "SELECT COUNT(*) FROM tabs WHERE instance_id='main'") == [(0,)]
        # It is journalled as a deferral against the unreachable home, as before flagging.
        assert await _rows(
            db, "SELECT instance_to, reason, decision FROM actions WHERE status='deferred'"
        ) == [("ghost", "1", "target_not_ready")]
    finally:
        await db.close()


async def test_deferred_rows_name_their_cause(tmp_path):
    """§7 defines `deferred` as connection/epoch/merge-caused. The same-url hold-back
    (the curator serialising itself, §7 "дубли ... не допускаются") wears the same status
    but is a different thing, so the rows carry the cause in ``decision``.
    curator_deferred_total sums every deferred row per target, so the split changes no
    metric. Reddens if both causes are written as one row."""
    db = await _mkdb(tmp_path)
    try:
        await _seed_rule(db, "grafana.lc", "main")
        ext = Ext(db)
        await ext.add_instance("main", tabs=[])
        # Two identical tabs -> one opens, one is held back as a same-pass duplicate.
        await ext.add_instance("prox", tabs=[
            _tabinfo(20, "https://grafana.lc/d/x"),
            _tabinfo(21, "https://grafana.lc/d/x"),
        ])
        ext.responder = lambda i, c, p: (
            {"ok": True, "result": {"tabId": ext.next_open_id(), "windowId": 1}}
            if c == protocol.CMD_OPEN_TAB else {"ok": True, "result": {}}
        )

        await ext.run_pass()
        rows = await _rows(
            db, "SELECT instance_to, reason, decision FROM actions "
                "WHERE status='deferred' ORDER BY decision")
        assert rows == [("main", "1", "dup_same_pass")]
        # Exactly one copy was opened for the two identical sources.
        assert await _rows(db, "SELECT COUNT(*) FROM actions WHERE kind='relocate' AND status='done'") == [(1,)]
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
