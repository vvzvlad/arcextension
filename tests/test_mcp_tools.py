"""MCP tool handlers (§11), unit-tested directly against the reused logic.

Covers each tool's handler plus the guards the reviewer mutation-checks:
* confirm_impact on an MCP rule write,
* execute_js audited (initiator='mcp' + the MCP session as auth_ctx),
* relocate_tab writing a live ``relocate`` row (initiator='mcp'),
* a stopped system refusing a mutating verb (and no command leaving the socket).
"""

import asyncio
import re
import sqlite3
from conftest import make_settings
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api import exemptions as exemptions_api
from src.curator import lease
from src.curator import pause as pause_ops
from src.db import state as state_read
from src.db.access import Database
from src.db.audit import insert_js_audit  # noqa: F401  (schema presence)
from src.db.settings_store import get_setting, set_setting
from src.ext import protocol
from src.ext.commands import resolve_response
from src.ext.registry import ConnState, Registry
from src.mcpiface import tools
from src.settings import EXT_WAIT_MAX_TIMEOUT_MS


# --- fakes / fixtures --------------------------------------------------------
class FakeWS:
    """Records every frame sent; async like the real socket."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_json(self, msg):
        if self.fail:
            raise RuntimeError("socket is gone")
        self.sent.append(msg)


def _settings(**over):
    """This file's settings, from the shared surface in ``tests/conftest.py``.

    No ``tmp_path``: nothing here builds an app — these tests call the functions
    directly and open their own ``Database`` — so the factory leaves ``db_path`` /
    ``backup_dir`` off entirely rather than inventing one.
    """
    return make_settings(**{**{"cmd_timeout_ms": 1000, "snapshot_timeout_ms": 200, "state_fresh_ms": 3000}, **over})
async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


def _app(db, registry=None, settings=None):
    registry = registry if registry is not None else Registry()
    settings = settings if settings is not None else _settings()
    return SimpleNamespace(
        state=SimpleNamespace(db=db, ext_registry=registry, settings=settings)
    )


def _put_conn(registry, instance_id, session_id="sess-1", ws=None):
    ws = ws or FakeWS()
    cs = ConnState(ws=ws, conn_epoch=1, install_uuid="u", session_id=session_id)
    registry.put(instance_id, cs)
    return cs, ws


async def _run_with_response(coro_factory, cs, ws, response_result):
    """Run a command-issuing handler concurrently and feed the correlated response."""
    start = len(ws.sent)  # wait for a NEW frame (ws.sent accumulates across calls)
    task = asyncio.create_task(coro_factory())
    for _ in range(400):
        if len(ws.sent) > start:
            break
        await asyncio.sleep(0.005)
    assert len(ws.sent) > start, "handler never sent a command frame"
    frame = ws.sent[-1]
    resolve_response(
        cs, {"type": "response", "id": frame["id"], "ok": True, "result": response_result}
    )
    return await task, frame


async def _insert_instance(db, iid, *, session_id=None, snapshot_at=None, connected=0,
                           focused_window_id=None):
    def _w(c):
        c.execute(
            "INSERT INTO instances (id, connected, session_id, snapshot_at, "
            "focused_window_id, status) VALUES (?, ?, ?, ?, ?, 'active')",
            (iid, connected, session_id, snapshot_at, focused_window_id),
        )
    await db.write(_w)


async def _insert_tab(db, iid, tab_id, *, url, title="t", opened_at=1000,
                      last_active_at=1000, age_unknown=0, now=2000, window_id=1,
                      fav_icon_url=None, pinned=0, audible=0, active=0):
    def _w(c):
        c.execute(
            "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, fav_icon_url, "
            "pinned, active, audible, opened_at, last_active_at, age_unknown, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, tab_id, window_id, url, title, fav_icon_url, pinned, active, audible,
             opened_at, last_active_at, age_unknown, now),
        )
    await db.write(_w)


async def _drive(coro_factory, conns, responder, *, timeout=5.0):
    """Run a MULTI-command handler (e.g. the synchronous relocate_tab, #48), answering
    every frame on whichever instance socket it lands on until the handler returns.

    ``conns`` maps instance_id -> (ConnState, FakeWS). ``responder(instance, cmd, params)``
    returns a result dict to answer ``ok:true`` OR ``("err", code)`` to refuse with that §6
    code. Returns ``(handler_return_value, [(instance, frame), ...])``. Re-raises whatever
    the handler raises (so a ``ToolError`` still surfaces to ``pytest.raises``)."""
    import time as _t

    seen = {iid: 0 for iid in conns}
    frames = []
    task = asyncio.create_task(coro_factory())
    deadline = _t.monotonic() + timeout
    while not task.done() and _t.monotonic() < deadline:
        for iid, (cs, ws) in conns.items():
            while len(ws.sent) > seen[iid]:
                frame = ws.sent[seen[iid]]
                seen[iid] += 1
                frames.append((iid, frame))
                verdict = responder(iid, frame["command"], frame.get("params", {}))
                if isinstance(verdict, tuple) and verdict and verdict[0] == "err":
                    resolve_response(cs, {"type": "response", "id": frame["id"],
                                          "ok": False,
                                          "error": {"code": verdict[1], "message": verdict[1]}})
                else:
                    resolve_response(cs, {"type": "response", "id": frame["id"],
                                          "ok": True, "result": verdict or {}})
        await asyncio.sleep(0.002)
    out = await task
    return out, frames


async def _insert_window(db, iid, window_id, *, wtype="normal", state="normal"):
    def _w(c):
        c.execute(
            "INSERT INTO windows (instance_id, window_id, type, state) VALUES (?,?,?,?)",
            (iid, window_id, wtype, state),
        )
    await db.write(_w)


# --- reads -------------------------------------------------------------------
async def test_list_instances_returns_freshness_envelope_and_stopped_at(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", snapshot_at=1234, connected=0)
    app = _app(db)
    out = await tools.list_instances(app)
    assert out["stopped_at"] is None
    # `instances` is now a per-instance freshness envelope keyed by id (§6/§11), not a
    # list of mirror rows. No live socket for "main" => ensure_fresh reports disconnected.
    main = out["instances"]["main"]
    # session_id rides the envelope (#47); "main" was inserted with no session_id.
    # The mirror-row fields the envelope replaced ride along too: dropping them would
    # leave `last_seen_at` / `reject_reason` / `reject_at` with no MCP surface at all.
    # The §11 capability report rides the same envelope: what this copy ALLOWS, as it
    # declared in its last hello. An instance that has never said hello reports the column
    # defaults — the single JS & Debugger switch OFF, version unknown — which is the honest
    # answer.
    assert main == {"snapshot_at": 1234, "fresh": False, "reason": "disconnected",
                    "session_id": None, "connected": False, "last_seen_at": None,
                    "reject_reason": None, "reject_at": None,
                    "allow_execute_js": False,
                    "ext_version": None}

    # A stopped curator surfaces stopped_at so an agent does not read the stop as a break.
    now = tools._now_ms()
    await db.write(lambda c: pause_ops.stop(c, now=now))
    out2 = await tools.list_instances(app)
    assert out2["stopped_at"] == now


async def test_list_tabs_returns_tabs_and_per_instance_freshness(tmp_path):
    db = await _make_db(tmp_path)
    now = tools._now_ms()
    # A live socket + a snapshot younger than STATE_FRESH_MS => ensure_fresh answers
    # `fresh` without sending or waiting for anything.
    await _insert_instance(db, "main", session_id="sess-1", snapshot_at=now, connected=1)
    await _insert_tab(db, "main", 1, url="https://a")
    reg = Registry()
    _put_conn(reg, "main", session_id="sess-1")
    out = await tools.list_tabs(_app(db, reg))
    assert [t["tab_id"] for t in out["tabs"]] == [1]
    assert out["instances"] == {
        "main": {"snapshot_at": now, "fresh": True, "reason": "fresh",
                 "session_id": "sess-1", "connected": True, "last_seen_at": None,
                 "reject_reason": None, "reject_at": None,
                 "allow_execute_js": False, "ext_version": None}
    }


async def test_list_tabs_freshens_and_flags_a_disconnected_sibling(tmp_path):
    # Acceptance (a): the fan-out is over known_instance_ids (every status='active'
    # instance), NOT "only connected" — so an instance with no live socket is REPORTED
    # as disconnected rather than silently dropped, and its siblings still come back.
    db = await _make_db(tmp_path)
    now = tools._now_ms()
    await _insert_instance(db, "main", session_id="sess-1", snapshot_at=now, connected=1)
    await _insert_instance(db, "media", session_id="sess-2", snapshot_at=None, connected=0)
    await _insert_tab(db, "main", 1, url="https://a")
    reg = Registry()
    _put_conn(reg, "main", session_id="sess-1")  # only "main" has a live socket
    out = await tools.list_tabs(_app(db, reg))
    inst = out["instances"]
    assert set(inst) == {"main", "media"}
    assert inst["main"] == {"snapshot_at": now, "fresh": True, "reason": "fresh",
                            "session_id": "sess-1", "connected": True,
                            "last_seen_at": None, "reject_reason": None, "reject_at": None,
                            "allow_execute_js": False, "ext_version": None}
    assert inst["media"] == {"snapshot_at": None, "fresh": False, "reason": "disconnected",
                             "session_id": "sess-2", "connected": False,
                             "last_seen_at": None, "reject_reason": None, "reject_at": None,
                             "allow_execute_js": False, "ext_version": None}
    # The disconnected sibling did not drop the fresh instance's tab.
    assert [t["tab_id"] for t in out["tabs"]] == [1]


async def test_list_tabs_reflects_a_snapshot_that_lands_during_the_call(tmp_path):
    """Acceptance 1 of #44: the tab a human opened is in the SAME response.

    This is the whole point of the issue — `list_tabs` used to kick a refresh and
    return the mirror it already had, so a freshly opened tab appeared only on the
    NEXT call. The test drives the real path: the mirror is stale, `ensure_fresh`
    sends a `snapshot_request`, the extension answers by applying a snapshot that
    carries a new tab, and the tool must return that tab.

    It is deliberately written to fail if the tabs are read BEFORE the fan-out —
    the exact shape of the original bug.
    """
    db = await _make_db(tmp_path)
    now = tools._now_ms()
    # Stale: snapshot_at far older than STATE_FRESH_MS, so ensure_fresh must ask.
    await _insert_instance(db, "main", session_id="sess-1", snapshot_at=now - 600_000,
                           connected=1)
    await _insert_tab(db, "main", 1, url="https://already-there")

    class LandingWS(FakeWS):
        """Answers a snapshot_request the way the channel does: applies the snapshot
        (new tab + refreshed snapshot_at) and frees the pending slot."""

        def __init__(self):
            super().__init__()
            self.conn_state = None

        async def send_json(self, frame):
            await super().send_json(frame)
            if frame.get("type") != protocol.TYPE_SNAPSHOT_REQUEST:
                return
            landed = tools._now_ms()

            def _apply(c):
                c.execute(
                    "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, "
                    "pinned, active, audible, opened_at, last_active_at, age_unknown, "
                    "updated_at) VALUES (?,?,?,?,?,0,0,0,?,?,0,?)",
                    ("main", 2, 1, "https://opened-by-a-human", "new", landed, landed,
                     landed),
                )
                c.execute(
                    "UPDATE instances SET snapshot_at = ?, last_seen_at = ? WHERE id = ?",
                    (landed, landed, "main"),
                )

            await db.write(_apply)
            self.conn_state.pending_snapshot_id = None

    ws = LandingWS()
    reg = Registry()
    cs, _ = _put_conn(reg, "main", session_id="sess-1", ws=ws)
    ws.conn_state = cs

    out = await tools.list_tabs(_app(db, reg))

    # The request actually went out...
    assert any(f.get("type") == protocol.TYPE_SNAPSHOT_REQUEST for f in ws.sent)
    # ...and the tab that landed during the call is in THIS response.
    assert sorted(t["tab_id"] for t in out["tabs"]) == [1, 2]
    assert out["instances"]["main"]["fresh"] is True
    assert out["instances"]["main"]["reason"] == "fresh"


async def test_freshen_fleet_spends_the_snapshot_timeout_budget(tmp_path, monkeypatch):
    """The budget is SETTINGS.snapshot_timeout_ms — the owner's «10 с», not a literal.

    Pins the value so shrinking it (or hard-coding a small one) is a red test rather
    than a silently impatient tool.
    """
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", session_id="sess-1", snapshot_at=None, connected=0)
    seen: list = []

    async def _spy(registry, db_, iid, settings, *, budget_ms=None):
        seen.append(budget_ms)
        return (False, "disconnected", None)

    monkeypatch.setattr(tools, "ensure_fresh", _spy)
    app = _app(db)
    await tools.list_tabs(app)
    assert seen == [app.state.settings.snapshot_timeout_ms]


async def test_list_tabs_reader_error_maps_to_error_and_isolates_siblings(tmp_path, monkeypatch):
    # Acceptance (b): ensure_fresh awaits db.read UNWRAPPED, so a reader fault
    # (database is locked / disk I/O) propagates. The fan-out runs under
    # gather(return_exceptions=True), so that fault is mapped to reason="error" for the
    # one instance and does NOT sink the tool or leave siblings hanging.
    db = await _make_db(tmp_path)
    now = tools._now_ms()
    await _insert_instance(db, "main", session_id="sess-1", snapshot_at=now, connected=1)
    await _insert_instance(db, "bad", session_id="sess-2", snapshot_at=42, connected=1)
    reg = Registry()
    _put_conn(reg, "main", session_id="sess-1")
    _put_conn(reg, "bad", session_id="sess-2")  # live socket => ensure_fresh reaches the reader

    import src.api.freshness as fresh_mod
    real = fresh_mod.read_instance_freshness

    def _maybe_raise(conn, instance_id):
        if instance_id == "bad":
            raise sqlite3.OperationalError("database is locked")
        return real(conn, instance_id)

    monkeypatch.setattr(fresh_mod, "read_instance_freshness", _maybe_raise)

    out = await tools.list_tabs(_app(db, reg))
    inst = out["instances"]
    # The faulting instance is reported, not lost; snapshot_at still comes from the
    # (separate, working) mirror read.
    assert inst["bad"] == {"snapshot_at": 42, "fresh": False, "reason": "error",
                           "session_id": "sess-2", "connected": True,
                           "last_seen_at": None, "reject_reason": None, "reject_at": None,
                           "allow_execute_js": False, "ext_version": None}
    # The sibling with a working reader is returned normally.
    assert inst["main"]["reason"] == "fresh" and inst["main"]["fresh"] is True


async def test_list_instances_exposes_pending_plan(tmp_path):
    # Acceptance (c): list_instances additionally carries the resume/pending-plan shape
    # the startpage's StateResponse already sees — verbatim _parse_pending_plan of the
    # same latch — which the agent previously only saw as the bare resume_pending bool.
    db = await _make_db(tmp_path)
    app = _app(db)
    assert (await tools.list_instances(app))["pending_plan"] is None

    raw = '{"since": 7, "plan": {"relocations": 2, "closures": 3, "deferred": {}}}'
    await db.write(lambda c: set_setting(c, pause_ops.RESUME_PENDING_KEY, raw))
    out = await tools.list_instances(app)
    assert out["pending_plan"] == state_read._parse_pending_plan(raw)
    assert out["pending_plan"]["plan"]["closures"] == 3
    assert out["resume_pending"] is True


async def test_get_rules_and_list_actions(tmp_path):
    db = await _make_db(tmp_path)
    from src.rules import access as ra
    await db.write(lambda c: ra.insert_rule(c, pattern="x.com", instance_id="main", created_at=1))
    rules = await tools.get_rules(_app(db))
    assert rules["rules"][0]["pattern"] == "x.com"

    from src.db.actions import insert_action
    await db.write(lambda c: insert_action(c, ts=10, kind="dedupe_close", status="done",
                                           initiator="curator", instance_from="main", url="https://a"))
    acts = await tools.list_actions(_app(db), kind="dedupe_close")
    assert acts["total"] == 1 and acts["items"][0]["kind"] == "dedupe_close"
    # A non-matching filter returns nothing.
    assert (await tools.list_actions(_app(db), kind="reset"))["total"] == 0


# --- rule writes: confirm_impact gate (§8/§11) -------------------------------
async def test_upsert_rule_requires_confirm_then_writes(tmp_path):
    db = await _make_db(tmp_path)
    app = _app(db)
    from src.rules import access as ra

    # Creating the FIRST rule crosses the empty boundary => momentous => gated.
    res = await tools.upsert_rule(app, rule={"pattern": "x.com", "instance_id": "main"})
    assert res["ok"] is False and res["requires_confirm"] is True
    assert (await db.read(ra.list_rules)) == []  # nothing written without confirm

    ok = await tools.upsert_rule(
        app, rule={"pattern": "x.com", "instance_id": "main"}, confirm_impact=True
    )
    assert ok["ok"] is True
    rows = await db.read(ra.list_rules)
    assert len(rows) == 1 and rows[0]["pattern"] == "x.com"


async def test_delete_rule_always_gated(tmp_path):
    db = await _make_db(tmp_path)
    app = _app(db)
    from src.rules import access as ra
    rid = await db.write(lambda c: ra.insert_rule(c, pattern="x.com", instance_id="main", created_at=1))

    res = await tools.delete_rule(app, rule_id=rid)
    assert res["ok"] is False and res["requires_confirm"] is True
    assert len(await db.read(ra.list_rules)) == 1  # still there

    ok = await tools.delete_rule(app, rule_id=rid, confirm_impact=True)
    assert ok["ok"] is True
    assert await db.read(ra.list_rules) == []


async def test_reset_singleton_navigates_and_journals_like_http(tmp_path):
    # MCP parity (§11): reset_singleton runs the SAME core as POST /api/rules/:id/reset
    # — a real navigate_tab plus an actions(kind='reset') row — and the ONLY difference
    # is initiator='mcp'. Reddens if the tool goes back to reporting an "intent".
    db = await _make_db(tmp_path)
    from src.rules import access as ra
    now = tools._now_ms()
    await _insert_instance(db, "main", session_id="sess-1", snapshot_at=now, connected=1)
    await _insert_tab(db, "main", 5, url="https://x.com/dash", last_active_at=now)
    rid = await db.write(lambda c: ra.insert_rule(
        c, pattern="x.com", instance_id="main", singleton=True,
        canonical_url="https://x.com/home", created_at=1))
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="sess-1")
    app = _app(db, reg)

    out, frame = await _run_with_response(
        lambda: tools.reset_singleton(app, rule_id=rid), cs, ws, {},
    )
    assert out["ok"] is True and out["reset"] is True and out["tab_id"] == 5
    assert frame["command"] == protocol.CMD_NAVIGATE_TAB
    assert frame["params"] == {"tabId": 5, "url": "https://x.com/home"}
    row = await db.read(lambda c: c.execute(
        "SELECT kind, status, initiator, instance_from, tab_id, url FROM actions"
    ).fetchone())
    assert row == ("reset", "done", "mcp", "main", 5, "https://x.com/home")


async def test_reset_singleton_missing_rule_is_a_tool_error(tmp_path):
    db = await _make_db(tmp_path)
    with pytest.raises(tools.ToolError) as ei:
        await tools.reset_singleton(_app(db), rule_id=999)
    assert ei.value.code == "not_found"


# --- commands: initiator='mcp' ----------------------------------------------
async def test_open_tab_sends_command(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.open_tab(app, instance="main", url="https://a", auth_ctx="mcp-sess"),
        cs, ws, {"tabId": 7, "windowId": 1},
    )
    assert out["ok"] is True and out["result"]["tabId"] == 7
    assert frame["command"] == protocol.CMD_OPEN_TAB and frame["params"]["url"] == "https://a"


# --- #45: open_tab window as address + server cross-check --------------------
async def test_open_tab_with_window_id_stamps_the_frame(tmp_path):
    # Acceptance 1: a named window rides the frame as `windowId`, and when the extension
    # answers with that same window the verb succeeds and echoes it.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.open_tab(app, instance="main", url="https://a", window_id=5, auth_ctx="s"),
        cs, ws, {"tabId": 7, "windowId": 5},
    )
    assert frame["params"]["windowId"] == 5
    assert out["ok"] is True and out["result"]["windowId"] == 5


async def test_open_tab_window_id_mismatch_is_no_window(tmp_path):
    # Acceptance 4: an OLD extension ignores the unknown `windowId` key, drops the tab in
    # its OWN window and still answers ok. The server cross-check compares the reported
    # windowId to the requested one and turns the miss into a loud `no_window`. Emulated
    # by having the extension answer a DIFFERENT windowId than requested.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await _run_with_response(
            lambda: tools.open_tab(app, instance="main", url="https://a", window_id=5),
            cs, ws, {"tabId": 7, "windowId": 9},  # NOT the requested 5
        )
    assert ei.value.code == protocol.ERR_NO_WINDOW


async def test_open_tab_without_window_id_omits_key_and_skips_cross_check(tmp_path):
    # Acceptance 5 / compatibility: no window_id => the frame is exactly today's (no
    # `windowId` key) and there is NO cross-check — the extension may report whatever
    # window its §9 auto-select chose, and the verb still succeeds.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.open_tab(app, instance="main", url="https://a"),
        cs, ws, {"tabId": 7, "windowId": 3},  # a window we never named — must NOT trip a check
    )
    assert "windowId" not in frame["params"]
    assert out["ok"] is True


async def test_close_and_focus_send_commands(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    _, f1 = await _run_with_response(
        lambda: tools.close_tab(app, instance="main", tab_id=3), cs, ws, {"ok": True})
    assert f1["command"] == protocol.CMD_CLOSE_TAB and f1["params"] == {"tabId": 3}
    _, f2 = await _run_with_response(
        lambda: tools.focus_tab(app, instance="main", tab_id=4), cs, ws, {"ok": True})
    assert f2["command"] == protocol.CMD_FOCUS_TAB and f2["params"] == {"tabId": 4}


# --- set_focus_emulation (§12, wave 18 — the chrome.debugger foundation) ------
async def test_set_focus_emulation_sends_enabled_and_returns_it(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)

    # enable: the flag rides into the frame and the extension's {enabled} comes back.
    out_on, f_on = await _run_with_response(
        lambda: tools.set_focus_emulation(app, instance="main", tab_id=9, enabled=True),
        cs, ws, {"enabled": True},
    )
    assert f_on["command"] == protocol.CMD_SET_FOCUS_EMULATION
    assert f_on["params"] == {"tabId": 9, "enabled": True}
    assert out_on == {"ok": True, "enabled": True}

    # disable: the false flag rides through and the response is reflected.
    out_off, f_off = await _run_with_response(
        lambda: tools.set_focus_emulation(app, instance="main", tab_id=9, enabled=False),
        cs, ws, {"enabled": False},
    )
    assert f_off["params"] == {"tabId": 9, "enabled": False}
    assert out_off == {"ok": True, "enabled": False}


# --- WebSocket-frame capture (§12, wave 21) ----------------------------------
async def test_start_ws_capture_sends_marker_and_returns_ok(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, f = await _run_with_response(
        lambda: tools.start_ws_capture(app, instance="main", tab_id=7),
        cs, ws, {"ok": True},
    )
    assert f["command"] == protocol.CMD_START_WS_CAPTURE
    # The synthetic audit marker rides into the frame (the verb carries no caller code).
    assert f["params"] == {"tabId": 7, "code": "[ws_capture:start]"}
    assert out == {"ok": True}


async def test_read_ws_frames_drains_and_reflects_dropped_url_remaining(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    frames = [
        {"dir": "recv", "opcode": 1, "ts": 1.0, "text": "hi"},
        {"dir": "sent", "opcode": 2, "ts": 2.0, "size": 9, "binary": True},
    ]
    out, f = await _run_with_response(
        lambda: tools.read_ws_frames(app, instance="main", tab_id=7, max_bytes=1000),
        cs, ws,
        {"frames": frames, "dropped": 3, "url": "wss://chat/socket", "remaining": 5},
    )
    assert f["command"] == protocol.CMD_READ_WS_FRAMES
    # max_bytes rides down as camelCase maxBytes (default 40000 is validated to a positive int).
    assert f["params"] == {"tabId": 7, "maxBytes": 1000}
    assert out == {"ok": True, "frames": frames, "dropped": 3,
                   "url": "wss://chat/socket", "remaining": 5}


async def test_read_ws_frames_defaults_and_fills_absent_fields(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    # An extension answering only with what it has: the tool fills the missing keys with safe
    # defaults (empty frames, zero dropped/remaining, url None), and default max_bytes rides down.
    out, f = await _run_with_response(
        lambda: tools.read_ws_frames(app, instance="main", tab_id=7),
        cs, ws, {},
    )
    assert f["params"] == {"tabId": 7, "maxBytes": tools.DEFAULT_MAX_BYTES}
    assert out == {"ok": True, "frames": [], "dropped": 0, "url": None, "remaining": 0}


async def test_read_ws_frames_rejects_bad_max_bytes_and_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.read_ws_frames(app, instance="main", tab_id=7, max_bytes=0)
    assert ei.value.code == "invalid_args"
    assert ws.sent == []  # refused before the round trip


async def test_stop_ws_capture_sends_command_and_returns_ok(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, f = await _run_with_response(
        lambda: tools.stop_ws_capture(app, instance="main", tab_id=7),
        cs, ws, {"ok": True},
    )
    assert f["command"] == protocol.CMD_STOP_WS_CAPTURE
    assert f["params"] == {"tabId": 7}
    assert out == {"ok": True}


async def test_start_ws_capture_refused_while_paused_sends_nothing(tmp_path):
    # start drives the browser (attach), so the stop gate refuses it and nothing reaches the
    # socket — same discipline as set_focus_emulation / start_js.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await db.write(lambda c: pause_ops.stop(c, now=tools._now_ms()))
    with pytest.raises(tools.ToolError) as ei:
        await tools.start_ws_capture(app, instance="main", tab_id=7)
    assert ei.value.code == "stopped"
    assert ws.sent == []


async def test_read_ws_frames_allowed_while_paused_still_drains(tmp_path):
    # A passive drain of the in-memory buffer must stay available under a stop, so an ALREADY-
    # captured conversation is not lost to ring eviction while the capture is still open. UNLIKE
    # start_ws_capture (browser-driving) read is NOT behind the stop gate — it never reaches the
    # browser. Without that, a paused curator would silently lose captured frames it could not drain.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await db.write(lambda c: pause_ops.stop(c, now=tools._now_ms()))
    frames = [{"dir": "recv", "opcode": 1, "ts": 1.0, "text": "hi"}]
    out, f = await _run_with_response(
        lambda: tools.read_ws_frames(app, instance="main", tab_id=7, max_bytes=1000),
        cs, ws,
        {"frames": frames, "dropped": 0, "url": "wss://chat/socket", "remaining": 0},
    )
    # It reached the round trip (was NOT refused with "stopped") and drained the buffer.
    assert f["command"] == protocol.CMD_READ_WS_FRAMES
    assert out == {"ok": True, "frames": frames, "dropped": 0,
                   "url": "wss://chat/socket", "remaining": 0}


async def test_stop_ws_capture_runs_even_while_paused(tmp_path):
    # Teardown must always be able to run: stop is NOT behind the stop gate, so a paused curator
    # can still end the «идёт отладка» exposure. (start IS gated; read and stop are not.)
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await db.write(lambda c: pause_ops.stop(c, now=tools._now_ms()))
    out, f = await _run_with_response(
        lambda: tools.stop_ws_capture(app, instance="main", tab_id=7),
        cs, ws, {"ok": True},
    )
    assert f["command"] == protocol.CMD_STOP_WS_CAPTURE
    assert out == {"ok": True}


# --- move_tab (§6/§9) --------------------------------------------------------
async def test_move_tab_sends_the_command_and_omits_an_absent_index(tmp_path):
    # The verb that closes §11's gap: relocation BETWEEN instances is the §7 open+close
    # pair (it works only because the browsers are separate processes), and between the
    # windows of ONE browser the agent had nothing at all.
    #
    # An absent index must be ABSENT from the frame, not sent as null or as a
    # server-invented -1: the extension owns the default ("append to the end"), and one
    # default living in two places is how the two halves drift.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)

    out, frame = await _run_with_response(
        lambda: tools.move_tab(app, instance="main", tab_id=7, window_id=3, auth_ctx="s"),
        cs, ws, {"tabId": 7, "windowId": 3, "index": -1},
    )
    assert frame["command"] == protocol.CMD_MOVE_TAB
    assert frame["params"] == {"tabId": 7, "windowId": 3}  # no `index` key at all
    assert out["ok"] is True and out["result"]["windowId"] == 3

    # An EXPLICIT index is passed through untouched, including 0.
    _, frame2 = await _run_with_response(
        lambda: tools.move_tab(app, instance="main", tab_id=7, window_id=3, index=0),
        cs, ws, {"tabId": 7, "windowId": 3, "index": 0},
    )
    assert frame2["params"] == {"tabId": 7, "windowId": 3, "index": 0}


async def test_move_tab_null_window_extracts_into_a_new_window(tmp_path):
    # #45 (acceptance 6, server half): window_id=None rides the frame verbatim as
    # `windowId: null` — the extract-into-a-new-window address — and the created window's
    # id comes back unchanged in the response. No `index` key: the new window's tab is its
    # only one and the extension owns the default.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.move_tab(app, instance="main", tab_id=7, window_id=None, auth_ctx="s"),
        cs, ws, {"tabId": 7, "windowId": 500, "index": 0},
    )
    assert frame["command"] == protocol.CMD_MOVE_TAB
    assert frame["params"] == {"tabId": 7, "windowId": None}  # null on the wire, no index
    assert out["ok"] is True and out["result"]["windowId"] == 500


async def test_move_tab_surfaces_the_pinned_refusal_as_its_own_code(tmp_path):
    # §9: a pinned tab never crosses a window boundary. The refusal must reach the agent
    # as a MACHINE-readable code it can act on (unpin, or move inside the window), not as
    # a generic failure — so the extension's `pinned_cross_window` travels verbatim
    # through CommandError into the ToolError the MCP wrapper renders as
    # {"ok": false, "error": "pinned_cross_window"}.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)

    task = asyncio.create_task(
        tools.move_tab(app, instance="main", tab_id=7, window_id=3, auth_ctx="s")
    )
    for _ in range(400):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert ws.sent, "move_tab never sent a command frame"
    resolve_response(cs, {
        "type": "response", "id": ws.sent[-1]["id"], "ok": False,
        "error": {"code": protocol.ERR_PINNED_CROSS_WINDOW, "message": "pinned"},
    })
    with pytest.raises(tools.ToolError) as ei:
        await task
    assert ei.value.code == protocol.ERR_PINNED_CROSS_WINDOW


async def test_move_tab_is_refused_while_stopped_and_sends_nothing(tmp_path):
    # A move is automation like every other mutating verb (§7/§12), and the MCP door has
    # no `force`: an agent is not a human at the keyboard.
    db = await _make_db(tmp_path)
    reg = Registry()
    _cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app)
    with pytest.raises(tools.ToolError) as ei:
        await tools.move_tab(app, instance="main", tab_id=7, window_id=3)
    assert ei.value.code == "stopped"
    assert ws.sent == []


# --- execute_js: audited (§12) ----------------------------------------------
async def test_execute_js_audited_with_mcp_session_as_auth_ctx(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, _ = await _run_with_response(
        lambda: tools.execute_js(
            app, instance="main", tab_id=2, code="1+1",
            url_at_exec="https://a", auth_ctx="mcp-session-xyz",
        ),
        cs, ws, {"value": 2},
    )
    assert out["ok"] is True
    rows = await db.read(lambda c: c.execute(
        "SELECT initiator, auth_ctx, code, outcome, url_at_exec FROM js_audit"
    ).fetchall())
    assert len(rows) == 1
    initiator, auth_ctx, code, outcome, url = rows[0]
    # §12: the js_audit trace names the MCP SESSION, not a token, and initiator='mcp'.
    assert initiator == "mcp" and auth_ctx == "mcp-session-xyz"
    assert code == "1+1" and outcome == "ok" and url == "https://a"


# --- scroll_until: FIXED verb, wait-budget socket, snake_case shape ----------
async def test_scroll_until_sends_command_and_renames_shape(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.scroll_until(
            app, instance="main", tab_id=3, count_selector=".msg",
            container_selector="#feed", direction="up", target_count=200,
            stable_rounds=4, interval_ms=500, focus=True, auth_ctx="s",
        ),
        cs, ws, {"count": 200, "rounds": 12, "stopped": "target", "elapsedMs": 6000},
    )
    assert frame["command"] == protocol.CMD_SCROLL_UNTIL
    p = frame["params"]
    assert p["countSelector"] == ".msg" and p["containerSelector"] == "#feed"
    assert p["direction"] == "up" and p["targetCount"] == 200
    assert p["stableRounds"] == 4 and p["intervalMs"] == 500 and p["focus"] is True
    assert isinstance(p["timeoutMs"], int) and p["timeoutMs"] > 0
    # elapsedMs -> elapsed_ms; the rest carried through.
    assert out == {"ok": True, "count": 200, "rounds": 12, "stopped": "target",
                   "elapsed_ms": 6000}
    # A fixed verb writes NO audit row.
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM js_audit").fetchone()) == (0,)


async def test_scroll_until_omits_optional_keys_and_defaults_direction(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.scroll_until(app, instance="main", tab_id=3, count_selector=".msg"),
        cs, ws, {"count": 10, "rounds": 3, "stopped": "stable", "elapsedMs": 2100},
    )
    p = frame["params"]
    assert "containerSelector" not in p and "targetCount" not in p and "focus" not in p
    assert p["direction"] == "down" and p["stableRounds"] == 3 and p["intervalMs"] == 700
    assert out["stopped"] == "stable" and out["count"] == 10


async def test_scroll_until_rejects_bad_direction_and_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.scroll_until(app, instance="main", tab_id=3, count_selector=".m",
                                 direction="sideways")
    assert ei.value.code == "invalid_args"
    assert ws.sent == []


async def test_scroll_until_refused_while_paused_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app)
    with pytest.raises(tools.ToolError) as ei:
        await tools.scroll_until(app, instance="main", tab_id=3, count_selector=".m")
    assert ei.value.code == "stopped"
    assert ws.sent == []


# --- Job-API: start_js (audited) + poll_job (fixed) --------------------------
async def test_start_js_mints_job_id_audits_and_returns_it(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.start_js(app, instance="main", tab_id=2, code="await scrape()",
                               world="MAIN", url_at_exec="https://a", auth_ctx="mcp-sess-j"),
        cs, ws, {"jobId": None},  # the extension echoes back; None => server's minted id wins
    )
    assert frame["command"] == protocol.CMD_START_JS
    minted = frame["params"]["jobId"]
    assert minted.startswith("job-")
    assert frame["params"]["code"] == "await scrape()" and frame["params"]["world"] == "MAIN"
    # start_js ALWAYS runs the code as an awaited async body, so the frame (and the js_audit
    # row it drives) unconditionally carries awaitPromise=true — there is no MCP-surface
    # parameter to toggle it, and the audit cannot misstate how the code ran.
    assert frame["params"]["awaitPromise"] is True
    assert out == {"ok": True, "job_id": minted}
    # Audited like execute_js: initiator='mcp', the MCP session as auth_ctx, outcome ok.
    row = await db.read(lambda c: c.execute(
        "SELECT initiator, auth_ctx, code, outcome, url_at_exec FROM js_audit").fetchone())
    assert row == ("mcp", "mcp-sess-j", "await scrape()", "ok", "https://a")


async def test_start_js_refused_while_paused_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app)
    with pytest.raises(tools.ToolError) as ei:
        await tools.start_js(app, instance="main", tab_id=2, code="x", auth_ctx="s")
    assert ei.value.code == "stopped"
    assert ws.sent == []
    # Refused BEFORE the audit-writing send, so no row either.
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM js_audit").fetchone()) == (0,)


async def test_poll_job_returns_state_and_caps_value(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    # done + a value under the cap: passed straight through.
    out, frame = await _run_with_response(
        lambda: tools.poll_job(app, instance="main", tab_id=2, job_id="job-1"),
        cs, ws, {"state": "done", "value": {"n": 3}},
    )
    assert frame["command"] == protocol.CMD_POLL_JOB
    assert frame["params"] == {"tabId": 2, "jobId": "job-1"}
    assert out == {"ok": True, "state": "done", "value": {"n": 3}}
    # A fixed verb writes NO audit row.
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM js_audit").fetchone()) == (0,)

    # A value over max_bytes is cut and flagged with the true size.
    big = "x" * 5000
    out2, _ = await _run_with_response(
        lambda: tools.poll_job(app, instance="main", tab_id=2, job_id="job-1", max_bytes=100),
        cs, ws, {"state": "done", "value": big},
    )
    assert out2["state"] == "done" and out2["truncated"] is True
    assert out2["total_bytes"] == 5000 and len(out2["value"]) == 100


async def test_poll_job_unknown_and_error_states(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    # unknown: the page-resident state is gone (reload/discard/close, or a wrong id).
    out, _ = await _run_with_response(
        lambda: tools.poll_job(app, instance="main", tab_id=2, job_id="gone"),
        cs, ws, {"state": "unknown"},
    )
    assert out == {"ok": True, "state": "unknown"}  # no value, no message
    # error carries the message, not a value.
    out2, _ = await _run_with_response(
        lambda: tools.poll_job(app, instance="main", tab_id=2, job_id="job-2"),
        cs, ws, {"state": "error", "message": "boom"},
    )
    assert out2 == {"ok": True, "state": "error", "message": "boom"}


async def test_poll_job_world_rides_the_frame(tmp_path):
    # A job started in ISOLATED lives in that world's global, so poll_job must be able to say
    # which world to read: `world` (when given) goes on the frame verbatim so the extension's
    # readJobInWorld inject lands where start_js wrote the record. MAIN default matches
    # start_js's default, so an unqualified pair puts no `world` on the wire.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.poll_job(app, instance="main", tab_id=2, job_id="job-9", world="ISOLATED"),
        cs, ws, {"state": "running"},
    )
    assert frame["command"] == protocol.CMD_POLL_JOB
    assert frame["params"] == {"tabId": 2, "jobId": "job-9", "world": "ISOLATED"}
    assert out == {"ok": True, "state": "running"}


# --- relocate_tab (#48): synchronous open + close in one call ----------------
def _reloc_responder(open_id=99):
    """A default extension responder for a synchronous relocate: open the copy with
    ``open_id``, answer get_tab, and let the source close succeed."""
    def _r(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            return {"tabId": open_id, "windowId": 1}
        return {"ok": True}  # get_tab (copy check) + close_tab both succeed
    return _r


async def test_relocate_tab_writes_relocate_action_initiator_mcp(tmp_path):
    # Acceptance 2: the synchronous verb journals the PAIR relocate(done) +
    # relocate_close(done) — the close half linked via origin_action_id, both under ONE
    # `mcp-` pass_id.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main-db")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash", opened_at=100, last_active_at=200)
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main-live")
    app = _app(db, reg)

    out, frames = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5,
                                   instance_to="main", auth_ctx="mcp-s"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _reloc_responder(99),
    )
    assert out["ok"] is True and out["status"] == "done" and out["tab_id_to"] == 99
    assert out["undo_pass_id"].startswith("mcp-")
    # open_tab + get_tab to the target, close_tab to the source (three frames).
    cmds = [(iid, f["command"]) for iid, f in frames]
    assert cmds == [("main", protocol.CMD_OPEN_TAB), ("main", protocol.CMD_GET_TAB),
                    ("themed", protocol.CMD_CLOSE_TAB)]

    rows = await db.read(lambda c: c.execute(
        "SELECT kind, status, initiator, instance_from, instance_to, tab_id, tab_id_to, "
        "session_id_from, session_id_to, origin_action_id, pass_id, url "
        "FROM actions ORDER BY id"
    ).fetchall())
    reloc, close = rows
    assert reloc[:9] == ("relocate", "done", "mcp", "themed", "main", 5, 99,
                         "s-themed", "s-main-live")
    assert close[:7] == ("relocate_close", "done", "mcp", "themed", "main", 5, 99)
    assert close[9] == out["action_id"]            # origin_action_id -> the relocate row
    assert reloc[10] == close[10] == out["undo_pass_id"]  # ONE mcp- pass_id for the pair
    # The source mirror row is GONE (the sync close removed it); the copy stays in target.
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed'").fetchone()) == (0,)
    assert await db.read(lambda c: c.execute(
        "SELECT url FROM tabs WHERE instance_id='main' AND tab_id=99").fetchone()) == (
        "https://grafana/dash",)


async def test_relocate_tab_untouched_source_completes_done_and_leaves_source(tmp_path):
    # Acceptance 1: an untouched tab relocates with status:"done"; list_tabs right after
    # does not show it under the source instance.
    db = await _make_db(tmp_path)
    now = tools._now_ms()
    await _insert_instance(db, "themed", session_id="s-themed", snapshot_at=now, connected=1)
    await _insert_instance(db, "main", session_id="s-main", snapshot_at=now, connected=1)
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    out, _ = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5,
                                   instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _reloc_responder(77),
    )
    assert out["status"] == "done"
    # list_tabs freshens the fleet; answer any snapshot the freshen fires, then assert the
    # source no longer lists tab 5.
    lt = await tools.list_tabs(app, instance="themed")
    assert [t["tab_id"] for t in lt["tabs"]] == []


# --- #47 session epoch: session_id out + expected_session stamped ------------
async def _extension_answers(cs, ws, *, browser_session, ok_result=None):
    """Wait for the handler's frame, then answer like the EXTENSION edge does (§5,
    extension/src/commands.js): the STAMPED sessionId is checked against the browser's
    LIVE session and a mismatch is refused with stale_session. This is what turns a stale
    stamp into the error the agent sees — the service itself never compares, it only
    stamps. Returns the frame so a test can assert exactly WHICH session was stamped."""
    for _ in range(400):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert ws.sent, "handler never sent a command frame"
    frame = ws.sent[-1]
    if frame.get("sessionId") == browser_session:
        resolve_response(cs, {"type": "response", "id": frame["id"], "ok": True,
                              "result": ok_result or {"ok": True}})
    else:
        resolve_response(cs, {"type": "response", "id": frame["id"], "ok": False,
                              "error": {"code": protocol.ERR_STALE_SESSION,
                                        "message": "foreign session"}})
    return frame


async def test_envelope_session_id_equals_instances_session_id(tmp_path):
    # Acceptance 1: list_instances / list_tabs carry session_id per ACTIVE instance, equal
    # to the stored instances.session_id.
    db = await _make_db(tmp_path)
    now = tools._now_ms()
    await _insert_instance(db, "main", session_id="sess-A", snapshot_at=now, connected=1)
    await _insert_instance(db, "media", session_id="sess-B", snapshot_at=now, connected=1)
    reg = Registry()
    _put_conn(reg, "main", session_id="sess-A")
    _put_conn(reg, "media", session_id="sess-B")
    app = _app(db, reg)

    li = await tools.list_instances(app)
    lt = await tools.list_tabs(app)
    for out in (li, lt):
        assert out["instances"]["main"]["session_id"] == "sess-A"
        assert out["instances"]["media"]["session_id"] == "sess-B"
    # ...and it is literally the DB column value, not the live-socket value.
    db_sessions = await db.read(state_read._read_active_sessions)
    assert db_sessions == {"main": "sess-A", "media": "sess-B"}


async def test_close_tab_old_expected_session_after_restart_is_stale(tmp_path):
    # Acceptance 2: the agent read sess-OLD; the browser restarted (both sides now on
    # sess-NEW). close_tab pinned to sess-OLD stamps sess-OLD, the extension rejects it =>
    # stale_session, and the tab is NOT closed (the fake extension performed no close).
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="sess-NEW")  # live = restarted session
    app = _app(db, reg)
    task = asyncio.create_task(
        tools.close_tab(app, instance="main", tab_id=3, expected_session="sess-OLD")
    )
    frame = await _extension_answers(cs, ws, browser_session="sess-NEW")
    # The frame carried the PINNED old session, not the live one.
    assert frame["sessionId"] == "sess-OLD"
    with pytest.raises(tools.ToolError) as ei:
        await task
    assert ei.value.code == protocol.ERR_STALE_SESSION


async def test_close_tab_without_expected_session_goes_through(tmp_path):
    # Acceptance 3: omit expected_session and the verb behaves exactly as today — the LIVE
    # session is stamped and the command runs. Pins the deliberate fail-open.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="sess-NEW")
    app = _app(db, reg)
    task = asyncio.create_task(tools.close_tab(app, instance="main", tab_id=3))
    frame = await _extension_answers(cs, ws, browser_session="sess-NEW")
    assert frame["sessionId"] == "sess-NEW"  # live session stamped, as before
    out = await task
    assert out["ok"] is True


async def test_close_tab_matching_expected_session_runs(tmp_path):
    # Acceptance 4: a matching expected_session stamps that session and the command runs.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="sess-NEW")
    app = _app(db, reg)
    task = asyncio.create_task(
        tools.close_tab(app, instance="main", tab_id=3, expected_session="sess-NEW")
    )
    frame = await _extension_answers(cs, ws, browser_session="sess-NEW")
    assert frame["sessionId"] == "sess-NEW"
    out = await task
    assert out["ok"] is True


async def test_expected_session_stamps_passed_not_live_after_reconnect(tmp_path):
    # Acceptance 5: a reconnect swaps the ConnState/session in the registry between the
    # agent reading the session and the send. Because the PASSED session is stamped (not
    # re-read from the now-live ConnState), the stale one still travels => stale_session.
    # A version that stamped the LIVE session would stamp the reconnected sess-NEW and
    # WRONGLY pass — so this is the mutation that must redden.
    db = await _make_db(tmp_path)
    reg = Registry()
    _put_conn(reg, "main", session_id="sess-OLD")            # what the agent read
    cs_new, ws_new = _put_conn(reg, "main", session_id="sess-NEW")  # a reconnect swapped it
    app = _app(db, reg)
    task = asyncio.create_task(
        tools.close_tab(app, instance="main", tab_id=3, expected_session="sess-OLD")
    )
    frame = await _extension_answers(cs_new, ws_new, browser_session="sess-NEW")
    assert frame["sessionId"] == "sess-OLD"  # the PASSED session, NOT the live sess-NEW
    with pytest.raises(tools.ToolError) as ei:
        await task
    assert ei.value.code == protocol.ERR_STALE_SESSION


async def test_relocate_tab_mismatched_expected_session_from_refuses(tmp_path):
    # Acceptance 6: a mismatched expected_session_from means the SOURCE browser restarted
    # since the agent read the session; relocate refuses and opens NOTHING — no copy row in
    # `tabs`, no `relocate` row in `actions` — because the guard fires BEFORE phase A.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    _cs_to, ws = _put_conn(reg, "main", session_id="s-main")  # target socket
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.relocate_tab(
            app, instance_from="themed", tab_id=5, instance_to="main",
            expected_session_from="s-OLD",
        )
    assert ei.value.code == protocol.ERR_STALE_SESSION
    assert ws.sent == []  # phase A never opened a copy in the target
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='main'").fetchone()) == (0,)
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM actions").fetchone()) == (0,)


async def test_relocate_tab_matching_expected_session_from_proceeds(tmp_path):
    # Pins the source guard as NON-vacuous AND the #47 stamp on the sync source close: a
    # MATCHING expected_session_from relocates fully. The TARGET open/get_tab carry the
    # live target session; the SOURCE close STAMPS expected_session_from.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)
    out, frames = await _drive(
        lambda: tools.relocate_tab(
            app, instance_from="themed", tab_id=5, instance_to="main",
            expected_session_from="s-themed",
        ),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _reloc_responder(99),
    )
    assert out["ok"] is True and out["status"] == "done" and out["tab_id_to"] == 99
    by_cmd = {(iid, f["command"]): f for iid, f in frames}
    # The target open stamped the LIVE target session, never expected_session_from.
    assert by_cmd[("main", protocol.CMD_OPEN_TAB)]["sessionId"] == "s-main"
    # The source close STAMPED expected_session_from (#47), NOT the live source session.
    close = by_cmd[("themed", protocol.CMD_CLOSE_TAB)]
    assert close["sessionId"] == "s-themed"
    assert close["params"]["expect"] == {
        "url": "https://grafana/dash", "notAudible": True, "notPinned": True,
    }  # NO minIdleMs — the agent chose this tab (#48)
    row = await db.read(lambda c: c.execute(
        "SELECT session_id_from FROM actions WHERE kind='relocate'").fetchone())
    assert row == ("s-themed",)


async def test_relocate_source_reconnect_after_precheck_stamps_the_passed_session(tmp_path):
    # #47/#48 NON-VACUITY for the sync close stamp: guard-3 passes (expected == live source
    # session), THEN the source browser reconnects with a NEW session BEFORE the close is
    # sent. The close must stamp the epoch the agent PASSED (s-themed), NOT the now-live
    # one — otherwise a tab_id from the dead epoch would be acted on. Unlike the matching
    # test (where expected == live, so the two stamps are indistinguishable), this makes
    # live != expected at close time: it reddens if the close stamps conn_state.session_id.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    def _responder(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            return {"tabId": 99, "windowId": 1}
        if cmd == protocol.CMD_GET_TAB:
            # Source browser restarts AFTER guard-3 read s-themed, BEFORE the close send.
            cs_from.session_id = "s-NEW"
            return {"ok": True}
        return {"ok": True}  # close_tab succeeds at the emulated edge

    _out, frames = await _drive(
        lambda: tools.relocate_tab(
            app, instance_from="themed", tab_id=5, instance_to="main",
            expected_session_from="s-themed",
        ),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _responder,
    )
    by_cmd = {(iid, f["command"]): f for iid, f in frames}
    close = by_cmd[("themed", protocol.CMD_CLOSE_TAB)]
    assert close["sessionId"] == "s-themed"  # the PASSED epoch, not the now-live s-NEW


async def _no_new_rows(db):
    """Assert no copy landed in the target and no action row was journalled."""
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='main'").fetchone()) == (0,)
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM actions").fetchone()) == (0,)


async def test_relocate_pinned_source_refused_before_open(tmp_path):
    # Acceptance 5: a PINNED source is refused with precondition_failed BEFORE open_tab —
    # phase B's notPinned guard could never close it, so nothing is opened or written.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash", pinned=1)
    reg = Registry()
    _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main")
    assert ei.value.code == protocol.ERR_PRECONDITION_FAILED
    assert ws_to.sent == []  # no open_tab ever left for the target
    await _no_new_rows(db)


async def test_relocate_audible_source_refused_before_open(tmp_path):
    # Acceptance 6: an AUDIBLE source is refused before open (notAudible guard).
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash", audible=1)
    reg = Registry()
    _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main")
    assert ei.value.code == protocol.ERR_PRECONDITION_FAILED
    assert ws_to.sent == []
    await _no_new_rows(db)


async def test_relocate_active_in_focused_window_refused_before_open(tmp_path):
    # Guard 4 (#48), active-in-focus arm: a tab ACTIVE in the source's FOCUSED window is
    # un-closeable by phase B, so it is refused before open. Non-vacuity: the SAME tab in a
    # NON-focused window (or non-active) relocates — covered by the happy-path tests.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed", focused_window_id=7)
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash", active=1, window_id=7)
    reg = Registry()
    _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main")
    assert ei.value.code == protocol.ERR_PRECONDITION_FAILED
    assert ws_to.sent == []
    await _no_new_rows(db)


async def test_relocate_active_but_not_in_focused_window_is_allowed(tmp_path):
    # The active guard is SCOPED to the focused window: an active tab in a NON-focused
    # window still relocates (else the guard would refuse far too much). Reddens if the
    # guard drops the window comparison.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed", focused_window_id=1)
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash", active=1, window_id=9)
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)
    out, _ = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _reloc_responder(42),
    )
    assert out["status"] == "done"


async def test_relocate_copy_gone_between_open_and_check_is_half(tmp_path):
    # Acceptance 7: the copy vanishes between step 3 and the copy check (get_tab =>
    # no_such_tab). The verb degrades to status:"half", reason:"copy_gone", and NEVER
    # touches the source — no close_tab is sent and the source tab stays in the mirror.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    def responder(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            return {"tabId": 99, "windowId": 1}
        if cmd == protocol.CMD_GET_TAB:
            return ("err", protocol.ERR_NO_SUCH_TAB)  # copy already gone
        raise AssertionError(f"unexpected command {cmd} — the source must not be touched")

    out, frames = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main"),
        {"main": (cs_to, ws_to)}, responder,
    )
    assert out["status"] == "half" and out["reason"] == "copy_gone"
    assert [f["command"] for _, f in frames] == [protocol.CMD_OPEN_TAB, protocol.CMD_GET_TAB]
    # Source untouched: its mirror row survives; the relocate row is live, the close pending.
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed' AND tab_id=5").fetchone()) == (1,)
    statuses = await db.read(lambda c: c.execute(
        "SELECT kind, status FROM actions ORDER BY id").fetchall())
    assert statuses == [("relocate", "done"), ("relocate_close", "pending")]


async def test_relocate_target_disconnected_open_fails_no_rows(tmp_path):
    # Acceptance 8: the target has no live socket. open_tab fails no_connection and NOTHING
    # is written (no copy tab, no action rows). The source tab (the seed) is untouched.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    _put_conn(reg, "themed", session_id="s-themed")  # source live; target has NO conn
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main")
    assert ei.value.code == protocol.ERR_NO_CONNECTION
    await _no_new_rows(db)
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed'").fetchone()) == (1,)


async def test_relocate_source_closed_by_someone_completes_done(tmp_path):
    # Acceptance 9: the source is closed by someone between the steps, so the source close
    # returns no_such_tab. The goal is reached: relocate_close => done (reason no_such_tab),
    # the source mirror row is dropped, and the relocation is NOT left live.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    def responder(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            return {"tabId": 99, "windowId": 1}
        if cmd == protocol.CMD_GET_TAB:
            return {"ok": True}
        return ("err", protocol.ERR_NO_SUCH_TAB)  # the source close: already gone

    out, _ = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)}, responder,
    )
    assert out["status"] == "done"
    close = await db.read(lambda c: c.execute(
        "SELECT status, reason FROM actions WHERE kind='relocate_close'").fetchone())
    assert close == ("done", protocol.ERR_NO_SUCH_TAB)
    # Source row dropped; the relocation is retired (a done relocate_close => not live).
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed' AND tab_id=5").fetchone()) == (0,)
    from src.curator.mirror import load_mirror
    assert (await db.read(load_mirror)).live_relocations == []


async def test_relocate_precondition_failed_on_close_is_half(tmp_path):
    # Step-6 precondition_failed (source turned pinned/audible/active after the pre-check):
    # relocate_close => failed, response half; a FAILED close leaves the relocation live so
    # the pass's phase B retries it.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    def responder(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            return {"tabId": 99, "windowId": 1}
        if cmd == protocol.CMD_GET_TAB:
            return {"ok": True}
        return ("err", protocol.ERR_PRECONDITION_FAILED)

    out, _ = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)}, responder,
    )
    assert out["status"] == "half" and out["reason"] == protocol.ERR_PRECONDITION_FAILED
    close = await db.read(lambda c: c.execute(
        "SELECT status, reason FROM actions WHERE kind='relocate_close'").fetchone())
    assert close == ("failed", protocol.ERR_PRECONDITION_FAILED)
    # A failed relocate_close does NOT retire the relocation (mirror.py): still live.
    from src.curator.mirror import load_mirror
    assert len((await db.read(load_mirror)).live_relocations) == 1


async def test_relocate_connection_class_close_stays_pending_half(tmp_path):
    # Step-6 close returns a CONNECTION-class code (no_connection/timeout/stale_session):
    # the close is UNKNOWN, so the relocate_close stays PENDING (NOT failed, NOT done) and
    # the response is half — reconcile finishes it. This is the data-loss-adjacent branch:
    # a regression that dropped the source, marked done, or set failed would pass every
    # other test. The source row must remain, and the PENDING close excludes the relocate
    # from live_relocations (so phase B does not double-close it before reconcile).
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    def responder(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            return {"tabId": 99, "windowId": 1}
        if cmd == protocol.CMD_GET_TAB:
            return {"ok": True}
        return ("err", protocol.ERR_NO_CONNECTION)  # source close: UNKNOWN

    out, _ = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)}, responder,
    )
    assert out["status"] == "half" and out["reason"] == protocol.ERR_NO_CONNECTION
    close = await db.read(lambda c: c.execute(
        "SELECT status FROM actions WHERE kind='relocate_close'").fetchone())
    assert close == ("pending",)  # NOT failed, NOT done — reconcile will resolve it
    # The source tab was NOT dropped (its close is uncertain).
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed' AND tab_id=5").fetchone()) == (1,)
    # A PENDING relocate_close excludes the relocation from live_relocations — phase B will
    # not double-close it; reconcile (read_pending_closes) picks the row up later.
    from src.curator.mirror import load_mirror
    assert len((await db.read(load_mirror)).live_relocations) == 0


async def test_relocate_refused_while_paused_sends_nothing(tmp_path):
    # Acceptance 11: the verb refuses while the emergency stop is armed and sends no
    # frames. The stop is indefinite (curator_stopped_at); its refusal code is "stopped".
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    now = tools._now_ms()
    await db.write(lambda c: pause_ops.stop(c, now=now))
    reg = Registry()
    _cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    _cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main")
    assert ei.value.code == "stopped"
    assert ws_from.sent == [] and ws_to.sent == []  # no frame reached any socket
    await _no_new_rows(db)


# --- pause (stop) / resume (start) -------------------------------------------
async def test_pause_writes_setting_and_bumps_epoch(tmp_path):
    db = await _make_db(tmp_path)
    app = _app(db)
    before = await db.read(lease.read_lease)
    out = await tools.pause(app)
    assert out["ok"] is True and out["stopped_at"] is not None
    after = await db.read(lease.read_lease)
    assert after["epoch"] == before["epoch"] + 1          # bump_epoch fenced any pass
    assert await db.read(lambda c: get_setting(c, "curator_stopped_at")) == str(out["stopped_at"])

    # `minutes` is accepted for backward compatibility and IGNORED — the stop is
    # indefinite and idempotent on the timestamp.
    out2 = await tools.pause(app, minutes=10)
    assert out2["stopped_at"] == out["stopped_at"]

    await tools.resume(app)
    assert await db.read(pause_ops.read_stopped_at) is None


async def test_mcp_resume_runs_a_pass_like_the_http_twin(tmp_path):
    """§7/§11 parity: a manual start runs a pass IMMEDIATELY, whichever door it came
    through. The MCP tool used to stop after the settings write, so an agent's resume
    left the curator idle until the next tick — the same verb with two behaviours.
    Reddens (no `pass` key, no `passes` row) if the tool stops writing settings only."""
    db = await _make_db(tmp_path)
    app = _app(db)
    await tools.pause(app)

    out = await tools.resume(app)
    assert out["ok"] is True
    assert "ttl_shift_ms" in out
    # A REAL pass ran (no instances are connected here → no_ready_instances) and left
    # its row in `passes`, exactly like DELETE /api/pause does.
    assert out["pass"]["status"] == "no_ready_instances"
    assert await db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM passes").fetchone()
    ) == (1,)


async def test_mcp_resume_against_an_empty_world_leaves_the_latch_armed(tmp_path):
    """§7/§11 parity, the other half: the agent's resume against a fleet that answered
    NOTHING must not eat the armed plan.

    The tool is the same door as ``DELETE /api/pause`` (both run
    :func:`src.api.pause.resume_now`), whose pass recomputes the plan FROM the ready
    instances. Here zero instances are connected, so the recomputed plan is empty by
    construction — no evidence that the armed 30-action plan shrank. The pass runs,
    reports ``no_ready_instances``, and leaves the latch untouched (value included):
    clearing on an evidence-free pass would make ``resume_pending`` flap with fleet
    connectivity. Reddens if the runner goes back to clearing the latch on a pass
    with an empty ready set.
    """
    from src.db.settings_store import set_setting

    db = await _make_db(tmp_path)
    app = _app(db)
    latch = '{"since": 1, "plan": {"total": 30, "threshold": 20}}'
    await db.write(lambda c: set_setting(c, pause_ops.RESUME_PENDING_KEY, latch))
    assert (await tools.list_instances(app))["resume_pending"] is True

    out = await tools.resume(app)
    assert out["ok"] is True
    # A REAL pass ran (nothing is connected → no_ready_instances) ...
    assert out["pass"]["status"] == "no_ready_instances", out["pass"]
    # ... and the latch SURVIVED with its plan value UNCHANGED — not cleared, not
    # overwritten by the empty plan this pass computed.
    assert (await tools.list_instances(app))["resume_pending"] is True
    assert await db.read(
        lambda c: get_setting(c, pause_ops.RESUME_PENDING_KEY)
    ) == latch


async def test_list_instances_exposes_resume_pending(tmp_path):
    """§7's visibility rule for the latch: an armed over-threshold plan «выводится в
    статус-полосу и ждёт одного подтверждающего клика», and ``list_instances`` is the
    agent's window into the same fact.

    Without it an agent sees no relocations happening and concludes the curator is
    broken, when it is deliberately deferring an over-threshold plan behind a click."""
    db = await _make_db(tmp_path)
    app = _app(db)
    assert (await tools.list_instances(app))["resume_pending"] is False

    from src.db.settings_store import set_setting
    await db.write(lambda c: set_setting(
        c, pause_ops.RESUME_PENDING_KEY, '{"since": 1, "plan": {"closures": 3}}'
    ))
    assert (await tools.list_instances(app))["resume_pending"] is True

    # Cleared (as an executing pass would clear it) → flag drops again.
    await db.write(lambda c: set_setting(c, pause_ops.RESUME_PENDING_KEY, ""))
    assert (await tools.list_instances(app))["resume_pending"] is False


async def test_mcp_merge_windows_delegates_to_the_shared_core(tmp_path):
    # §9: one implementation behind the startpage button and the MCP tool. The tool
    # must surface the extension's `merged` count, not a bespoke shape.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.merge_windows(app, instance="main", auth_ctx="s"),
        cs, ws, {"merged": 4},
    )
    assert frame["command"] == protocol.CMD_MERGE_WINDOWS
    assert out["ok"] is True and out["merged"] == 4


async def test_merge_windows_forwards_expected_session_through_the_core(tmp_path):
    # #47: expected_session must reach send_command THROUGH the shared merge core, so
    # the frame stamps the PINNED session (not the live 'sess-1'). Reddens if the tool
    # or the core drops the forward — a mutation deleting expected_session=expected_session
    # at src/api/instances.py would then stamp the live session here.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")  # live session is 'sess-1'
    app = _app(db, reg)
    _out, frame = await _run_with_response(
        lambda: tools.merge_windows(app, instance="main", expected_session="pinned-99",
                                    auth_ctx="s"),
        cs, ws, {"merged": 1},
    )
    assert frame["sessionId"] == "pinned-99"


async def test_pause_refuses_mutating_verb_and_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app)  # arm the stop

    with pytest.raises(tools.ToolError) as ei:
        await tools.open_tab(app, instance="main", url="https://a", auth_ctx="s")
    assert ei.value.code == "stopped"
    assert ws.sent == []  # the mutating verb never reached the socket

    # A read is NOT gated by the stop.
    assert "instances" in await tools.list_instances(app)
    # After resume the same verb proceeds. (resume itself runs a pass — §7 — so it puts
    # its own snapshot_request on the socket; _run_with_response waits for a NEW frame.)
    await tools.resume(app)
    out, _frame = await _run_with_response(
        lambda: tools.open_tab(app, instance="main", url="https://a", auth_ctx="s"),
        cs, ws, {"tabId": 1},
    )
    assert out["ok"] is True


# --- run_pass ----------------------------------------------------------------
async def test_run_pass_dry_run_writes_nothing(tmp_path):
    db = await _make_db(tmp_path)
    out = await tools.run_pass(_app(db), dry_run=True)
    assert out["status"] == "dry_run"
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM actions").fetchone()) == (0,)
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM passes").fetchone()) == (0,)


# --- #46: list_tabs filters, dup marking, window summary, minus favicon ------
async def test_list_tabs_omits_fav_icon_url_but_state_reader_keeps_it(tmp_path):
    # Acceptance 1: fav_icon_url is projected OUT of list_tabs (no agent consumer), while
    # the shared reader (the /api/state / build_state path) still carries it — the drop
    # lives in the MCP adapter over the rows, NOT in the SQL surface.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", connected=0)
    await _insert_tab(db, "main", 1, url="https://a", fav_icon_url="https://a/favicon.ico")
    out = await tools.list_tabs(_app(db))
    assert [t["tab_id"] for t in out["tabs"]] == [1]
    assert all("fav_icon_url" not in t for t in out["tabs"])
    # The canonical SQL selection is untouched — the row still has the favicon.
    rows = await db.read(state_read._read_tabs)
    assert rows[0]["fav_icon_url"] == "https://a/favicon.ico"


async def test_build_state_reader_unbroken_by_new_filter_params(tmp_path):
    # Acceptance 4: build_state calls the SHARED _read_tabs with no filter args; the new
    # optional params must default to None so /api/state's reader is unbroken and still
    # exposes fav_icon_url.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", connected=0)
    await _insert_tab(db, "main", 1, url="https://a", fav_icon_url="f")
    state = await db.read(lambda c: state_read.build_state(c, 123))
    assert [t["tab_id"] for t in state["tabs"]] == [1]
    assert state["tabs"][0]["fav_icon_url"] == "f"


async def test_list_tabs_filters_by_instance_window_and_url(tmp_path):
    # Acceptance 2: each of the three filters narrows correctly (url_contains is
    # case-INsensitive).
    db = await _make_db(tmp_path)
    await _insert_instance(db, "prox", connected=0)
    await _insert_instance(db, "media", connected=0)
    await _insert_tab(db, "prox", 1, url="https://example.com/a", window_id=10)
    await _insert_tab(db, "prox", 2, url="https://www.YouTube.com/watch?v=1", window_id=20)
    await _insert_tab(db, "media", 3, url="https://other.com", window_id=30)
    app = _app(db)
    out = await tools.list_tabs(app, instance="prox")
    assert {t["instance_id"] for t in out["tabs"]} == {"prox"}
    assert {t["tab_id"] for t in out["tabs"]} == {1, 2}
    out = await tools.list_tabs(app, window_id=30)
    assert [t["tab_id"] for t in out["tabs"]] == [3]
    out = await tools.list_tabs(app, url_contains="youtube")
    assert [t["tab_id"] for t in out["tabs"]] == [2]


async def test_list_tabs_filters_intersect(tmp_path):
    # Acceptance 3: combined filters AND together.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "prox", connected=0)
    await _insert_instance(db, "media", connected=0)
    await _insert_tab(db, "prox", 1, url="https://example.com/keep", window_id=10)
    await _insert_tab(db, "prox", 2, url="https://example.com/keep", window_id=20)
    await _insert_tab(db, "media", 3, url="https://example.com/keep", window_id=10)
    out = await tools.list_tabs(_app(db), instance="prox", window_id=10)
    assert [(t["instance_id"], t["tab_id"]) for t in out["tabs"]] == [("prox", 1)]


async def test_dup_group_marks_shared_normalized_address(tmp_path):
    # Acceptance 5: two tabs differing only by query share the SAME non-empty dup_group
    # (the normalized address); a unique-key tab gets null.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", connected=0)
    await _insert_tab(db, "main", 1, url="https://x.com/p?a=1")
    await _insert_tab(db, "main", 2, url="https://x.com/p?a=2")
    await _insert_tab(db, "main", 3, url="https://y.com/q")
    out = await tools.list_tabs(_app(db))
    by_id = {t["tab_id"]: t for t in out["tabs"]}
    assert by_id[1]["dup_group"] == by_id[2]["dup_group"] == "https://x.com/p"
    assert by_id[3]["dup_group"] is None


async def test_dup_group_computed_after_filters(tmp_path):
    # Acceptance 6: dup_group is computed over the POST-filter output — narrowing to one
    # of a former pair makes that survivor the sole holder of its key => null.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", connected=0)
    await _insert_tab(db, "main", 1, url="https://x.com/p?a=1")
    await _insert_tab(db, "main", 2, url="https://x.com/p?a=2")
    out = await tools.list_tabs(_app(db), url_contains="a=1")
    assert [t["tab_id"] for t in out["tabs"]] == [1]
    assert out["tabs"][0]["dup_group"] is None


async def test_list_windows_summary_with_counts_and_focused_flag(tmp_path):
    # Acceptance 7: one record per window, correct tab_count + focused flag, and a
    # response that is a few hundred bytes (a per-window summary, not per-tab).
    import json

    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", connected=0)
    await db.write(lambda c: c.execute(
        "UPDATE instances SET focused_window_id = ? WHERE id = ?", (10, "main")))
    await _insert_window(db, "main", 10)
    await _insert_window(db, "main", 20, wtype="popup", state="minimized")
    await _insert_tab(db, "main", 1, url="https://a", window_id=10)
    await _insert_tab(db, "main", 2, url="https://b", window_id=10)
    await _insert_tab(db, "main", 3, url="https://c", window_id=20)
    out = await tools.list_windows(_app(db))
    by_win = {w["window_id"]: w for w in out["windows"]}
    assert by_win[10]["tab_count"] == 2 and by_win[10]["focused"] is True
    assert by_win[20]["tab_count"] == 1 and by_win[20]["focused"] is False
    assert by_win[20]["type"] == "popup" and by_win[20]["state"] == "minimized"
    assert len(json.dumps(out["windows"])) < 1000


async def test_window_tab_counts_sum_to_list_tabs_count(tmp_path):
    # Acceptance 8: sum of a window's tab counts == that instance's list_tabs count with
    # no filters, same moment.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "prox", connected=0)
    await _insert_window(db, "prox", 10)
    await _insert_window(db, "prox", 20)
    await _insert_tab(db, "prox", 1, url="https://a", window_id=10)
    await _insert_tab(db, "prox", 2, url="https://b", window_id=10)
    await _insert_tab(db, "prox", 3, url="https://c", window_id=20)
    app = _app(db)
    wins = await tools.list_windows(app)
    tabs = await tools.list_tabs(app, instance="prox")
    total = sum(w["tab_count"] for w in wins["windows"] if w["instance_id"] == "prox")
    assert total == len(tabs["tabs"]) == 3


async def test_read_tools_return_dict_no_structured_content_and_failure_shape(tmp_path):
    # Acceptance 9: the read tools stay dict-returning (structured_content ABSENT), and a
    # failure still surfaces as {"ok": false, "error": ...} rather than a transport fault.
    from src.mcpiface.server import build_mcp

    db = await _make_db(tmp_path)
    try:
        await _insert_instance(db, "main", connected=0)
        await _insert_window(db, "main", 1)
        await _insert_tab(db, "main", 1, url="https://a", window_id=1)
        app = _app(db)
        mcp = build_mcp(SimpleNamespace(app=app))
        for name in ("list_tabs", "list_windows"):
            res = await mcp.call_tool(name, {})
            # A dict return with no output schema => text content, structured_content None.
            assert res.structured_content is None
            assert res.is_error is False

        # The handlers themselves return plain dicts.
        assert isinstance(await tools.list_tabs(app), dict)
        assert isinstance(await tools.list_windows(app), dict)

        # Failure path: degraded mode makes _guarded return the structured refusal dict
        # {"ok": false, "error": ...} — still a dict, still no structured_content, not a
        # transport error.
        import json

        app.state.degraded = True
        res = await mcp.call_tool("list_tabs", {})
        assert res.structured_content is None
        payload = json.loads(res.content[0].text)
        assert payload["ok"] is False and payload["error"] == "degraded"
    finally:
        await db.close()


# --- §12 parity: degraded mode refuses tools (through the real _guarded wrapper) --
async def test_mcp_tools_refuse_in_degraded_mode(tmp_path):
    # A failed migration => degraded => /api/* returns 503 and the curator driver does
    # not run. The MCP layer must refuse too, or an agent could write against an
    # unverified schema. Exercised through the REGISTERED tool (build_mcp + _guarded),
    # not the bare handler, so it covers the wrapper's gate. Asserted by the side
    # effect (no rule written) — robust to call_tool's return wrapping.
    from src.mcpiface.server import build_mcp
    db = await _make_db(tmp_path)
    try:
        app = _app(db)
        app.state.degraded = True
        mcp = build_mcp(SimpleNamespace(app=app))
        try:
            await mcp.call_tool(
                "upsert_rule",
                {"rule": {"pattern": "x.com", "instance_id": "main"}, "confirm_impact": True},
            )
        except Exception:
            pass  # a refusal may surface as an error; the point is that nothing was written
        # Drop the _guarded degraded gate and this rule gets written => count becomes 1.
        n = await db.read(lambda c: c.execute("SELECT COUNT(*) FROM rules").fetchone())
        assert n[0] == 0
    finally:
        await db.close()


# --- #49 bulk verbs: ONE frame per list, per-item results --------------------
async def test_close_tab_bulk_three_ids_is_ONE_frame_with_per_item_results(tmp_path):
    # Acceptance 1 + 2: three tab_ids, one already closed -> array of three (two ok, the
    # gone one no_such_tab); and the whole call puts EXACTLY ONE frame on the socket.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="s1")
    app = _app(db, reg)
    ext = {"results": [
        {"index": 0, "ok": True, "tabId": 10},
        {"index": 1, "ok": False, "tabId": 11, "error": "no_such_tab"},
        {"index": 2, "ok": True, "tabId": 12},
    ]}
    out, frame = await _run_with_response(
        lambda: tools.close_tab(app, instance="main", tab_ids=[10, 11, 12], auth_ctx="s"),
        cs, ws, ext,
    )
    assert len(ws.sent) == 1  # acceptance 2: the core proof — ONE frame, not three
    assert frame["command"] == protocol.CMD_CLOSE_TAB
    assert frame["params"] == {"items": [{"tabId": 10}, {"tabId": 11}, {"tabId": 12}]}
    assert out == {"ok": True, "results": ext["results"]}


async def test_close_tab_bulk_short_result_array_filled_with_no_result(tmp_path):
    # The extension answered fewer results than items -> the missing index is filled
    # with {ok:false, error:"no_result"} (annotated with the requested tabId).
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="s1")
    app = _app(db, reg)
    ext = {"results": [{"index": 0, "ok": True, "tabId": 10}]}  # index 1 missing
    out, _ = await _run_with_response(
        lambda: tools.close_tab(app, instance="main", tab_ids=[10, 11], auth_ctx="s"),
        cs, ws, ext,
    )
    assert out["results"] == [
        {"index": 0, "ok": True, "tabId": 10},
        {"index": 1, "ok": False, "error": "no_result", "tabId": 11},
    ]


async def test_close_tab_single_form_keeps_prior_response_shape(tmp_path):
    # Acceptance 8: the single form (tab_id) is byte-for-byte the prior contract.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="s1")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.close_tab(app, instance="main", tab_id=5, auth_ctx="s"),
        cs, ws, {"ok": True},
    )
    assert frame["params"] == {"tabId": 5}  # single frame form unchanged
    assert out == {"ok": True, "result": {"ok": True}}


async def test_move_tab_single_form_keeps_prior_response_shape(tmp_path):
    # Acceptance 8: move_tab single form unchanged.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="s1")
    app = _app(db, reg)
    out, frame = await _run_with_response(
        lambda: tools.move_tab(app, instance="main", tab_id=7, window_id=3, index=0, auth_ctx="s"),
        cs, ws, {"tabId": 7, "windowId": 3, "index": 0},
    )
    assert frame["params"] == {"tabId": 7, "windowId": 3, "index": 0}
    assert out == {"ok": True, "result": {"tabId": 7, "windowId": 3, "index": 0}}


async def test_move_tab_bulk_is_one_frame_with_shared_window(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="s1")
    app = _app(db, reg)
    ext = {"results": [
        {"index": 0, "ok": True, "tabId": 1, "windowId": 3},
        {"index": 1, "ok": False, "tabId": 2, "error": "pinned_cross_window"},
    ]}
    out, frame = await _run_with_response(
        lambda: tools.move_tab(app, instance="main", tab_ids=[1, 2], window_id=3, auth_ctx="s"),
        cs, ws, ext,
    )
    assert len(ws.sent) == 1
    assert frame["params"] == {"items": [{"tabId": 1}, {"tabId": 2}], "windowId": 3}
    assert out == {"ok": True, "results": ext["results"]}


async def test_bulk_invalid_args_gates_send_no_frame(tmp_path):
    # Acceptance 9: all the invalid_args shapes refuse BEFORE the send.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="s1")
    app = _app(db, reg)
    bad = [
        lambda: tools.close_tab(app, instance="main", tab_id=1, tab_ids=[1]),  # both
        lambda: tools.close_tab(app, instance="main"),                          # neither
        lambda: tools.close_tab(app, instance="main", tab_ids=[]),              # empty
        lambda: tools.close_tab(app, instance="main", tab_ids=[1, 1]),          # duplicate
        lambda: tools.move_tab(app, instance="main", tab_id=1, tab_ids=[1], window_id=2),  # both
        lambda: tools.move_tab(app, instance="main", tab_ids=[1, 2], window_id=None),  # tab_ids+null
        lambda: tools.relocate_tab(app, instance_from="a", instance_to="b",
                                   tab_id=1, tab_ids=[1]),                        # both
        lambda: tools.relocate_tab(app, instance_from="a", instance_to="b", tab_ids=[]),  # empty
        lambda: tools.relocate_tab(app, instance_from="a", instance_to="b", tab_ids=[1, 1]),  # dup
    ]
    for call in bad:
        with pytest.raises(tools.ToolError) as ei:
            await call()
        assert ei.value.code == "invalid_args"
    assert ws.sent == []  # not one frame left, for any of them


async def test_bulk_verbs_refused_while_paused_send_nothing(tmp_path):
    # Acceptance 11: the pause gate refuses every bulk verb and sends no frame.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main", session_id="s1")
    app = _app(db, reg)
    await db.write(lambda c: pause_ops.stop(c, now=tools._now_ms()))
    calls = [
        lambda: tools.close_tab(app, instance="main", tab_ids=[1, 2]),
        lambda: tools.move_tab(app, instance="main", tab_ids=[1, 2], window_id=3),
        lambda: tools.relocate_tab(app, instance_from="main", instance_to="main", tab_ids=[1, 2]),
    ]
    for call in calls:
        with pytest.raises(tools.ToolError) as ei:
            await call()
        assert ei.value.code == "stopped"
    assert ws.sent == []


# --- #49 bulk relocate: two frames, dedup, one pass_id -----------------------
def _bulk_reloc_responder(open_ids):
    """Answer the bulk relocate's THREE frames: open_tab {items} assigns each opened copy a
    tabId from ``open_ids`` (in item order); get_tab {items} copy-check reports every copy
    present; close_tab {items} succeeds per item."""
    def _r(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            items = params.get("items", [])
            return {"results": [
                {"index": i, "ok": True, "tabId": open_ids[i], "windowId": 1}
                for i in range(len(items))
            ]}
        if cmd == protocol.CMD_GET_TAB:
            items = params.get("items", [])
            return {"results": [
                {"index": i, "ok": True, "tabId": it["tabId"]}
                for i, it in enumerate(items)
            ]}
        if cmd == protocol.CMD_CLOSE_TAB:
            items = params.get("items", [])
            return {"results": [
                {"index": i, "ok": True, "tabId": it["tabId"]}
                for i, it in enumerate(items)
            ]}
        return {"ok": True}
    return _r


async def test_relocate_bulk_two_frames_per_item_status_and_copies(tmp_path):
    # Acceptance 5: relocate a list -> per-item status; target holds copies of all.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://a/x", opened_at=100, last_active_at=200)
    await _insert_tab(db, "themed", 6, url="https://b/y", opened_at=100, last_active_at=200)
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    out, frames = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_ids=[5, 6],
                                   instance_to="main", auth_ctx="mcp-s"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _bulk_reloc_responder([101, 102]),
    )
    # EXACTLY three frames: ONE open to the target, ONE get_tab copy-check to the target,
    # ONE close to the source (#49 copy-check makes bulk relocate 3 frames, like #48 single).
    cmds = [(iid, f["command"]) for iid, f in frames]
    assert cmds == [("main", protocol.CMD_OPEN_TAB), ("main", protocol.CMD_GET_TAB),
                    ("themed", protocol.CMD_CLOSE_TAB)]
    assert out["ok"] is True
    assert [r["status"] for r in out["results"]] == ["done", "done"]
    assert all(r["ok"] for r in out["results"])
    # The target holds BOTH copies; both source rows are gone.
    urls = await db.read(lambda c: c.execute(
        "SELECT url FROM tabs WHERE instance_id='main' ORDER BY url").fetchall())
    assert [u[0] for u in urls] == ["https://a/x", "https://b/y"]
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed'").fetchone()) == (0,)


async def test_relocate_bulk_copy_gone_between_open_and_close_leaves_source(tmp_path):
    # FIX 1 (#49 data-loss guard): a target restart between the open and the close destroys
    # ONE copy. The get_tab copy-check catches it — that item is half/copy_gone, its SOURCE is
    # NOT closed (mirror row survives) and its relocate_close is left PENDING for reconcile;
    # the other item, whose copy is present, completes done and its source is dropped.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://a/x", opened_at=100, last_active_at=200)
    await _insert_tab(db, "themed", 6, url="https://b/y", opened_at=100, last_active_at=200)
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    def responder(iid, cmd, params):
        if cmd == protocol.CMD_OPEN_TAB:
            items = params["items"]  # copy 101 <- source 5, copy 102 <- source 6
            return {"results": [{"index": i, "ok": True, "tabId": 101 + i, "windowId": 1}
                                for i in range(len(items))]}
        if cmd == protocol.CMD_GET_TAB:
            # The copy-check: copy 102 (source tab 6) vanished (target restart); 101 present.
            out = []
            for i, it in enumerate(params["items"]):
                if it["tabId"] == 102:
                    out.append({"index": i, "ok": False, "tabId": 102,
                                "error": protocol.ERR_NO_SUCH_TAB})
                else:
                    out.append({"index": i, "ok": True, "tabId": it["tabId"]})
            return {"results": out}
        if cmd == protocol.CMD_CLOSE_TAB:
            # ONLY the confirmed copy's source (tab 5) may be closed — tab 6 must NOT appear.
            items = params["items"]
            assert all(it["tabId"] != 6 for it in items), "copy_gone source must not be closed"
            return {"results": [{"index": i, "ok": True, "tabId": it["tabId"]}
                                for i, it in enumerate(items)]}
        raise AssertionError(f"unexpected command {cmd}")

    out, frames = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_ids=[5, 6],
                                   instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        responder,
    )
    # Item 0 (copy present) done; item 1 (copy gone) half/copy_gone.
    assert out["results"][0]["status"] == "done"
    assert out["results"][1]["status"] == "half"
    assert out["results"][1]["reason"] == "copy_gone"
    # The copy_gone item's SOURCE (tab 6) is NOT deleted — its mirror row survives.
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed' AND tab_id=6").fetchone()) == (1,)
    # The done item's source (tab 5) IS gone.
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='themed' AND tab_id=5").fetchone()) == (0,)
    # The copy_gone item's relocate_close is still PENDING; the done item's is done.
    rows = await db.read(lambda c: c.execute(
        "SELECT tab_id, status FROM actions WHERE kind='relocate_close' ORDER BY tab_id"
    ).fetchall())
    assert rows == [(5, "done"), (6, "pending")]


async def test_relocate_bulk_dedup_same_url_drops_the_second(tmp_path):
    # Acceptance 6: two sources holding the SAME url into one target -> the second gets
    # error:"duplicate", exactly ONE copy in the target, and the open frame carried ONE item.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://dup/same", opened_at=100, last_active_at=200)
    await _insert_tab(db, "themed", 6, url="https://dup/same", opened_at=100, last_active_at=200)
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    out, frames = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_ids=[5, 6],
                                   instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _bulk_reloc_responder([201]),  # only ONE copy is ever opened
    )
    assert out["results"][0]["status"] == "done"
    assert out["results"][1] == {"index": 1, "ok": False, "error": "duplicate", "tab_id": 6}
    # Exactly ONE copy landed in the target.
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM tabs WHERE instance_id='main'").fetchone()) == (1,)
    # Dedup happened BEFORE the send: the open frame carried a single item.
    open_frame = next(f for iid, f in frames if f["command"] == protocol.CMD_OPEN_TAB)
    assert len(open_frame["params"]["items"]) == 1


async def test_relocate_bulk_dedup_against_target_existing_url(tmp_path):
    # Dedup is also against what the TARGET already holds, not only within the batch.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://already/there", opened_at=100, last_active_at=200)
    await _insert_tab(db, "main", 900, url="https://already/there")  # target already holds it
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    out, frames = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_ids=[5],
                                   instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _bulk_reloc_responder([]),
    )
    assert out["results"][0] == {"index": 0, "ok": False, "error": "duplicate", "tab_id": 5}
    assert out["undo_pass_id"] is None  # nothing opened => no pass
    # No frame was ever sent (the only item was deduped before phase A).
    assert [c for _iid, c in [(iid, f["command"]) for iid, f in frames]] == []


async def test_relocate_bulk_one_undo_pass_id_reverses_the_whole_batch(tmp_path):
    # Acceptance 7: the batch shares ONE undo_pass_id; undo sees it as one unit-set.
    from src.api.undo import _classify, _read_pass_actions

    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://a/x", opened_at=100, last_active_at=200)
    await _insert_tab(db, "themed", 6, url="https://b/y", opened_at=100, last_active_at=200)
    reg = Registry()
    cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)

    out, _ = await _drive(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_ids=[5, 6],
                                   instance_to="main"),
        {"themed": (cs_from, ws_from), "main": (cs_to, ws_to)},
        _bulk_reloc_responder([101, 102]),
    )
    pid = out["undo_pass_id"]
    assert pid and pid.startswith("mcp-")
    rows = await db.read(lambda c: _read_pass_actions(c, pid))
    # All FOUR rows (2 relocate + 2 relocate_close) share the ONE pass_id.
    kinds = sorted((r["kind"], r["status"]) for r in rows)
    assert kinds == [("relocate", "done"), ("relocate", "done"),
                     ("relocate_close", "done"), ("relocate_close", "done")]
    # No `passes` row is written for a synthetic mcp- pass.
    assert await db.read(lambda c: c.execute(
        "SELECT COUNT(*) FROM passes WHERE pass_id=?", (pid,)).fetchone()) == (0,)
    # Undo classifies the batch as TWO units and requires confirm (impact>0), like a pass.
    cls = _classify(rows)
    assert cls["relocations"] == 2
    assert cls["reopens"] == 2 and cls["copy_closes"] == 2 and cls["impact"] > 0


# --- the shared result cap ---------------------------------------------------
def test_truncate_payload_cuts_a_string_and_reports_the_TRUE_total():
    """The cap exists so the agent never has to write ``.slice(0, 1500)`` by hand — it
    forgets exactly once, and one call then floods its context."""
    assert tools.truncate_payload("short", 100) == ("short", {})
    value, meta = tools.truncate_payload("x" * 50, 10)
    assert value == "x" * 10
    # total_bytes is the FULL size, which is what tells the agent to narrow its selector.
    assert meta == {"truncated": True, "total_bytes": 50}


def test_truncate_payload_never_ends_a_cut_string_in_a_broken_character():
    # Ten 2-byte characters; an 5-byte cut lands mid-sequence. The severed bytes are
    # dropped rather than decoded into U+FFFD.
    value, meta = tools.truncate_payload("Ω" * 10, 5)
    assert value == "ΩΩ"
    assert "�" not in value
    assert meta["total_bytes"] == 20


def test_truncate_payload_hands_back_a_non_str_as_a_marked_string_not_broken_json():
    """A cut JSON document is not valid JSON, so it comes back as a STRING under an
    explicit marker — the alternative is a structure that LOOKS parseable and is not."""
    big = {"rows": ["y" * 100 for _ in range(50)]}
    value, meta = tools.truncate_payload(big, 60)
    assert set(value) == {"__truncated_json"}
    assert len(value["__truncated_json"].encode()) <= 60
    assert meta["truncated"] is True and meta["total_bytes"] > 60
    # Under the limit the value is returned UNTOUCHED, still a real structure.
    assert tools.truncate_payload({"a": 1}, 1000) == ({"a": 1}, {})


def test_truncate_payload_refuses_a_useless_limit():
    with pytest.raises(tools.ToolError) as ei:
        tools.truncate_payload("x", 0)
    assert ei.value.code == "invalid_args"


def test_flatten_injection_results_picks_the_main_frame_and_keeps_the_rest():
    raw = [
        {"frameId": 7, "documentId": "d7", "result": "sub"},
        {"frameId": 0, "documentId": "d0", "result": "main"},
    ]
    out = tools._flatten_injection_results(raw, None)
    # frameId 0 is the main frame regardless of the order chrome reports them in.
    assert out["value"] == "main"
    assert out["frames"] == [
        {"frame_id": 7, "value": "sub"}, {"frame_id": 0, "value": "main"},
    ]
    # No frame 0 reported => the first entry, so the shape is never empty.
    assert tools._flatten_injection_results([{"frameId": 3, "result": "only"}], None)["value"] == "only"
    # Nothing at all is a null value, not a crash.
    assert tools._flatten_injection_results(None, None) == {"value": None}


def test_flatten_omits_frames_when_there_is_only_one():
    """One frame => no ``frames`` key, because it would just repeat ``value``.

    The extension targets ``{tabId}`` and never sets ``allFrames``, so ONE entry is what
    every call today produces. Emitting it anyway sends the same payload twice — and since
    the cap is applied per entry, a truncated answer would weigh 2x ``max_bytes``: a byte
    cap that doubles the payload it exists to bound.
    """
    out = tools._flatten_injection_results([{"frameId": 0, "result": "z" * 100}], 10)
    assert "frames" not in out
    assert out == {"value": "z" * 10, "truncated": True, "total_bytes": 100}


def test_flatten_caps_each_frame_separately(tmp_path):
    # A giant result in ONE sub-frame must not swallow the main frame's answer.
    raw = [{"frameId": 0, "result": "ok"}, {"frameId": 1, "result": "z" * 100}]
    out = tools._flatten_injection_results(raw, 10)
    assert out["value"] == "ok" and "truncated" not in out
    assert out["frames"][1]["truncated"] is True
    assert out["frames"][1]["total_bytes"] == 100
    # The main frame's already-cut value is REUSED, not truncated a second time.
    assert out["frames"][0] == {"frame_id": 0, "value": "ok"}


# --- execute_js: awaitPromise, the flat result, the caller-named budget -------
async def test_execute_js_returns_a_flat_value_instead_of_nested_injection_results(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, frame = await _run_with_response(
        lambda: tools.execute_js(_app(db, reg), instance="main", tab_id=2, code="1"),
        cs, ws,
        {"results": [{"frameId": 0, "documentId": "d", "result": {"title": "T"}}]},
    )
    # Was `{"result": {"results": [ ... ]}}` — JSON inside JSON inside JSON for ONE value.
    # And `frames` is ABSENT for a single frame rather than repeating `value` verbatim.
    assert out == {"ok": True, "value": {"title": "T"}}
    # awaitPromise is NOT on the wire unless asked for: an unchanged call must put an
    # unchanged frame on the socket for an older extension.
    assert "awaitPromise" not in frame["params"]


async def test_execute_js_await_promise_rides_the_frame_only_when_true(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    _out, frame = await _run_with_response(
        lambda: tools.execute_js(_app(db, reg), instance="main", tab_id=2,
                                 code="return await f()", await_promise=True),
        cs, ws, {"results": [{"frameId": 0, "result": 1}]},
    )
    assert frame["params"]["awaitPromise"] is True


async def test_execute_js_caps_the_value_at_max_bytes(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.execute_js(_app(db, reg), instance="main", tab_id=2, code="1",
                                 max_bytes=8),
        cs, ws, {"results": [{"frameId": 0, "result": "a" * 40}]},
    )
    assert out["value"] == "a" * 8
    assert out["truncated"] is True and out["total_bytes"] == 40


async def test_a_bad_max_bytes_is_refused_BEFORE_the_round_trip(tmp_path):
    """No frame on the socket, and for execute_js no js_audit row either.

    The cap is applied to the RESPONSE, so validating it there meant an invalid argument
    cost a full command — during which the extension, reading ``maxBytes <= 0`` as "no
    limit", ships the entire innerText over the socket — and only then heard ``invalid_args``.
    For ``execute_js`` it also left a durable audit row for code that was never delivered.
    """
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    for bad in (0, -1):
        with pytest.raises(tools.ToolError) as exc:
            await tools.get_text(_app(db, reg), instance="main", tab_id=2, max_bytes=bad)
        assert exc.value.code == "invalid_args"
        with pytest.raises(tools.ToolError) as exc:
            await tools.execute_js(_app(db, reg), instance="main", tab_id=2, code="1",
                                   max_bytes=bad)
        assert exc.value.code == "invalid_args"
    assert ws.sent == []
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM js_audit").fetchone()) == (0,)


def test_truncation_survives_a_LONE_SURROGATE(tmp_path):
    """A page can genuinely return one, and encoding it must not kill the tool call.

    ``"\\ud800".encode("utf-8")`` raises ``UnicodeEncodeError``, and ``_guarded`` catches
    only ToolError/HTTPException — so one malformed character on a page would escape into
    the transport. This path is NEW: before the cap the value passed straight through and
    the MCP layer encoded it with ``ensure_ascii=True``, where a surrogate is harmless.
    """
    lone = "before\ud800after"
    # Under the cap: returned untouched, exactly as any other string.
    assert tools.truncate_payload(lone, 1000) == (lone, {})
    # Over the cap: measured (the surrogate counts at its 3-byte width) and cut, never raised.
    value, meta = tools.truncate_payload(lone, 8)
    assert meta == {"truncated": True, "total_bytes": 14}
    assert isinstance(value, str)
    # Same for the JSON branch, which has its own encode.
    value, meta = tools.truncate_payload({"k": lone}, 8)
    assert meta["truncated"] is True and meta["total_bytes"] > 8


async def _capture_budget(monkeypatch, factory, result=None):
    """Run ``factory()`` capturing the ``cmd_timeout_ms`` the verb hands send_command.

    The budget is the ONE thing a response-driven test cannot observe (the frame carries
    no timeout), and for the waiting verbs it is the whole correctness argument.

    ``result`` is what the fake extension answers; the empty default is fine for every verb
    that only reads keys, but a verb that JUDGES the frame (``navigate_tab`` refuses one
    without ``matched``) needs a realistic one.
    """
    seen = {}

    async def _fake_send(registry, db, instance_id, command, params, *, cmd_timeout_ms, **kw):
        seen["cmd_timeout_ms"] = cmd_timeout_ms
        seen["params"] = params
        seen["command"] = command
        return {} if result is None else result

    monkeypatch.setattr(tools, "send_command", _fake_send)
    await factory()
    return seen


async def test_execute_js_timeout_is_clamped_to_the_ceiling(tmp_path, monkeypatch):
    db = await _make_db(tmp_path)
    app = _app(db, Registry(), _settings(cmd_timeout_ms=1000, execute_js_max_timeout_ms=5000))
    # Under the ceiling: honoured verbatim.
    seen = await _capture_budget(monkeypatch, lambda: tools.execute_js(
        app, instance="main", tab_id=1, code="1", timeout_ms=4000))
    assert seen["cmd_timeout_ms"] == 4000
    # Over it: clamped, never honoured — the operator's ENV is the ceiling, not a hint.
    seen = await _capture_budget(monkeypatch, lambda: tools.execute_js(
        app, instance="main", tab_id=1, code="1", timeout_ms=999_000))
    assert seen["cmd_timeout_ms"] == 5000
    # Absent: the GLOBAL budget, byte for byte as before.
    seen = await _capture_budget(monkeypatch, lambda: tools.execute_js(
        app, instance="main", tab_id=1, code="1"))
    assert seen["cmd_timeout_ms"] == 1000
    for bad in (0, -1, "5000", True):
        with pytest.raises(tools.ToolError):
            await tools.execute_js(app, instance="main", tab_id=1, code="1", timeout_ms=bad)


# --- get_text: the FIXED-function read (§12) ---------------------------------
async def test_get_text_returns_the_text_and_writes_NO_js_audit_row(tmp_path):
    """THE §12 line this wave draws. The execute_js gate (the extension-edge checkbox + an
    audit row before the send) exists because ARBITRARY code arrives there and truncated
    code cannot be reconstructed. get_text injects a function committed into the extension
    and known at build time — there is nothing to reconstruct, so it is NOT behind that
    gate and writes NO audit row. Every other gate still applies (stop, revoke,
    stale_session, the http/https edge guard).
    """
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, frame = await _run_with_response(
        lambda: tools.get_text(_app(db, reg), instance="main", tab_id=2, selector="#a"),
        cs, ws, {"text": "hello world"},
    )
    assert out == {"ok": True, "text": "hello world"}
    assert frame["command"] == protocol.CMD_GET_TEXT
    assert frame["params"] == {"tabId": 2, "maxBytes": tools.DEFAULT_MAX_BYTES, "selector": "#a"}
    # No arbitrary code ran, so there is nothing to audit — and an audit row here would be
    # a row with an empty `code` column, i.e. evidence of nothing.
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM js_audit").fetchone()) == (0,)


async def test_get_text_keeps_the_extensions_truncation_numbers(tmp_path):
    # The extension measured the WHOLE document, so its total_bytes is the real size; a
    # server-side re-measure could only report the size of what already arrived.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.get_text(_app(db, reg), instance="main", tab_id=2, max_bytes=4),
        cs, ws, {"text": "abcd", "truncated": True, "totalBytes": 99999},
    )
    # camelCase on the wire (§6), snake_case for the agent.
    assert out == {"ok": True, "text": "abcd", "truncated": True, "total_bytes": 99999}


async def test_get_text_never_reports_truncated_with_a_null_total(tmp_path):
    """An extension that flags ``truncated`` without a number falls back to our measurement.

    "truncated: true, total_bytes: null" is the tool saying "I cut it and I won't say by how
    much". The local number is a floor rather than the true size, but a floor is actionable
    and a null is not.
    """
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.get_text(_app(db, reg), instance="main", tab_id=2, max_bytes=64),
        cs, ws, {"text": "abcd", "truncated": True},  # no totalBytes at all
    )
    assert out == {"ok": True, "text": "abcd", "truncated": True, "total_bytes": 4}


async def test_get_text_cap_is_enforced_even_by_an_extension_that_ignores_maxBytes(tmp_path):
    # "New service + old extension" is a guaranteed state (they update by different
    # paths), so the server-side cap is what actually protects the agent's context.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.get_text(_app(db, reg), instance="main", tab_id=2, max_bytes=5),
        cs, ws, {"text": "x" * 500},  # no truncated flag: an old extension ignored maxBytes
    )
    assert out["text"] == "x" * 5
    assert out["truncated"] is True and out["total_bytes"] == 500


async def test_get_text_is_refused_while_stopped_and_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    _cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app)
    with pytest.raises(tools.ToolError) as ei:
        await tools.get_text(app, instance="main", tab_id=2)
    assert ei.value.code == "stopped"
    assert ws.sent == []


# --- set_input: the FIXED-but-MUTATING write (§12) ---------------------------
async def test_set_input_sends_selector_and_value_and_returns_kind(tmp_path):
    """FIXED body like get_text (no js_audit row), but a WRITE. selector+value ride as DATA,
    the extension answers `kind` (what it wrote), and the tool passes it straight through."""
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, frame = await _run_with_response(
        lambda: tools.set_input(_app(db, reg), instance="main", tab_id=2,
                                selector="#email", value="a@b.c"),
        cs, ws, {"kind": "input"},
    )
    assert out == {"ok": True, "kind": "input"}
    assert frame["command"] == protocol.CMD_SET_INPUT
    assert frame["params"] == {"tabId": 2, "selector": "#email", "value": "a@b.c"}
    # No arbitrary code ran, so there is nothing to audit — the same §12 line as get_text.
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM js_audit").fetchone()) == (0,)


async def test_set_input_carries_the_contenteditable_kind_through(tmp_path):
    # `kind` distinguishes what was written; the tool must not flatten it to a bare ok.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.set_input(_app(db, reg), instance="main", tab_id=2,
                                selector="[contenteditable]", value="hi"),
        cs, ws, {"kind": "contenteditable"},
    )
    assert out == {"ok": True, "kind": "contenteditable"}


async def test_set_input_is_refused_while_paused_and_sends_nothing(tmp_path):
    # A MUTATION: gated by the stop switch exactly like navigate_tab. The stop gates every
    # browser-reaching verb, get_text included (see test_get_text_is_refused_while_stopped above);
    # the mutation is what sets set_input apart, not the gate. The refusal must land BEFORE the
    # frame reaches the socket.
    db = await _make_db(tmp_path)
    reg = Registry()
    _cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app)
    with pytest.raises(tools.ToolError) as ei:
        await tools.set_input(app, instance="main", tab_id=2, selector="#a", value="x")
    assert ei.value.code == "stopped"
    assert ws.sent == []


# --- wait_for ----------------------------------------------------------------
async def test_wait_for_requires_exactly_one_predicate_and_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    _cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    for kwargs in (
        {},
        {"url_matches": "a", "selector": "#b"},
        {"url_matches": "a", "selector": "#b", "text_contains": "c"},
    ):
        with pytest.raises(tools.ToolError) as ei:
            await tools.wait_for(app, instance="main", tab_id=1, **kwargs)
        assert ei.value.code == "invalid_args"
    assert ws.sent == []  # "and" vs "or" is not a guess to make — nothing is polled


async def test_wait_for_socket_budget_OUTLASTS_the_page_deadline(tmp_path, monkeypatch):
    """THE ordering that makes the verb work.

    The extension polls until ITS deadline and only then answers ``timeout``. Give the
    socket the same budget and the command dies on the wire first — every wait would
    report the SERVICE's timeout instead of the page's answer, which is strictly worse
    than not having the verb.
    """
    db = await _make_db(tmp_path)
    app = _app(db, Registry(), _settings(cmd_timeout_ms=1000, execute_js_max_timeout_ms=30000))
    seen = await _capture_budget(monkeypatch, lambda: tools.wait_for(
        app, instance="main", tab_id=1, selector="#done", timeout_ms=8000))
    assert seen["params"]["timeoutMs"] == 8000
    assert seen["cmd_timeout_ms"] > 8000
    # And it is never SHORTER than the ordinary command budget either.
    seen = await _capture_budget(monkeypatch, lambda: tools.wait_for(
        app, instance="main", tab_id=1, selector="#done", timeout_ms=1))
    assert seen["cmd_timeout_ms"] >= 1000


def test_the_socket_slack_outlasts_a_whole_poll_interval():
    """``_WAIT_SLACK_MS`` must exceed the extension's poll period, not merely be positive.

    The extension checks its deadline only BETWEEN polls, so the last check can land up to
    one full WAIT_POLL_MS late — plus the round trip home. A slack smaller than that
    interval would let the socket give up while the answer is already on its way, turning
    a definite ``matched:false`` into "state unknown" for the calls that ran closest to
    their deadline: the hardest failure to reproduce, since it depends on timing alone.
    """
    constants_js = (
        Path(__file__).resolve().parent.parent / "extension" / "src" / "constants.js"
    ).read_text(encoding="utf-8")
    m = re.search(r"^export const WAIT_POLL_MS\s*=\s*(\d+);", constants_js, re.MULTILINE)
    assert m, "WAIT_POLL_MS not found in constants.js (moved or reformatted?)"
    assert tools._WAIT_SLACK_MS > int(m.group(1))


def test_the_default_ceiling_fits_inside_what_the_extension_will_honour():
    """``EXECUTE_JS_MAX_TIMEOUT_MS`` <= the extension's own WAIT_MAX_TIMEOUT_MS.

    Pinned on the DEFAULT here; ``tests/test_settings.py`` pins the bound for every
    configured value. Raise the default past the extension's ceiling and every wait longer
    than a minute would be silently shortened while the service reported the longer number.
    """
    settings = make_settings()
    assert settings.execute_js_max_timeout_ms <= EXT_WAIT_MAX_TIMEOUT_MS


async def test_wait_for_defaults_to_the_ceiling_and_clamps_to_it(tmp_path, monkeypatch):
    # A wait with no stated length wants the LONGEST the operator permits — where every
    # other verb wants the ordinary command budget.
    db = await _make_db(tmp_path)
    app = _app(db, Registry(), _settings(cmd_timeout_ms=1000, execute_js_max_timeout_ms=7000))
    seen = await _capture_budget(monkeypatch, lambda: tools.wait_for(
        app, instance="main", tab_id=1, url_matches="/done"))
    assert seen["params"]["timeoutMs"] == 7000
    seen = await _capture_budget(monkeypatch, lambda: tools.wait_for(
        app, instance="main", tab_id=1, url_matches="/done", timeout_ms=999_000))
    assert seen["params"]["timeoutMs"] == 7000


async def test_wait_for_passes_the_predicate_through_and_writes_no_audit(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, frame = await _run_with_response(
        lambda: tools.wait_for(_app(db, reg), instance="main", tab_id=2,
                               text_contains="Paid", timeout_ms=3000),
        cs, ws, {"matched": True, "elapsedMs": 500},
    )
    # `elapsedMs` is the wire spelling; everything the agent reads is snake_case.
    assert out == {"ok": True, "matched": True, "elapsed_ms": 500}
    assert frame["command"] == protocol.CMD_WAIT_FOR
    assert frame["params"] == {"tabId": 2, "timeoutMs": 3000, "textContains": "Paid"}
    # Fixed function, not eval => no js_audit row (see get_text's docstring).
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM js_audit").fetchone()) == (0,)


async def test_wait_for_reports_a_missed_condition_as_ok_matched_false(tmp_path):
    """The deadline passing is a VERDICT, not ``timeout``.

    §11 fixes ``timeout`` to mean UNKNOWN — no frame arrived, the browser may be wedged, do
    not blindly retry. A wait that ran its course is the opposite fact and must not wear the
    same name: the browser answered, and the answer is "no". The two are told apart by the
    response SHAPE, which is what survives the wire — an ``elapsedMs`` riding on an
    ``ok:false`` frame would not, since ``send_command`` discards ``result`` on any failure.
    """
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.wait_for(_app(db, reg), instance="main", tab_id=2,
                               selector="#done", timeout_ms=3000),
        cs, ws, {"matched": False, "elapsedMs": 3000},
    )
    assert out == {"ok": True, "matched": False, "elapsed_ms": 3000}


# --- navigate_tab waitUntil --------------------------------------------------
async def test_navigate_tab_without_wait_until_sends_the_frame_it_always_sent(tmp_path, monkeypatch):
    # The reset path calls this command; a new key on the wire by default would change
    # what every deployed extension receives.
    db = await _make_db(tmp_path)
    app = _app(db, Registry())
    seen = await _capture_budget(monkeypatch, lambda: tools.navigate_tab(
        app, instance="main", tab_id=2, url="https://a/"))
    assert seen["params"] == {"tabId": 2, "url": "https://a/"}
    # The GLOBAL budget, unchanged: naming no wait must not buy a longer socket.
    assert seen["cmd_timeout_ms"] == app.state.settings.cmd_timeout_ms
    seen = await _capture_budget(monkeypatch, lambda: tools.navigate_tab(
        app, instance="main", tab_id=2, url="https://a/", wait_until="none"))
    assert seen["params"] == {"tabId": 2, "url": "https://a/"}


async def test_navigate_tab_with_wait_until_carries_the_wait_and_a_longer_budget(tmp_path, monkeypatch):
    db = await _make_db(tmp_path)
    app = _app(db, Registry(), _settings(cmd_timeout_ms=1000, execute_js_max_timeout_ms=30000))
    seen = await _capture_budget(monkeypatch, lambda: tools.navigate_tab(
        app, instance="main", tab_id=2, url="https://a/", wait_until="selector",
        selector="#app", timeout_ms=6000), {"matched": True, "elapsedMs": 10})
    assert seen["params"] == {"tabId": 2, "url": "https://a/", "waitUntil": "selector",
                              "timeoutMs": 6000, "selector": "#app"}
    assert seen["cmd_timeout_ms"] > 6000  # same ordering rule as wait_for


async def test_navigate_tab_answers_in_wait_fors_shape_when_a_wait_was_asked_for(tmp_path):
    """One question, one shape. ``wait_for`` renames on purpose — ``elapsedMs`` is the WIRE
    spelling and everything the agent reads is snake_case — and passing the extension's
    frame through raw made the neighbouring verb answer camelCase, with a SECOND ``ok:true``
    nested inside an answer whose subject may be a condition that did not hold.

    Both budget tests above go through a fake ``send_command`` that answers ``{}``, so the
    response shape was pinned by nothing; this one hands over a realistic frame.
    """
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, frame = await _run_with_response(
        lambda: tools.navigate_tab(_app(db, reg), instance="main", tab_id=2,
                                   url="https://a/", wait_until="selector",
                                   selector="#app", timeout_ms=5000),
        cs, ws, {"ok": True, "matched": False, "elapsedMs": 5000},
    )
    assert out == {"ok": True, "matched": False, "elapsed_ms": 5000}
    assert frame["params"]["waitUntil"] == "selector"


async def test_navigate_tab_refuses_an_OLD_extension_with_its_OWN_code(tmp_path):
    """A frame with no ``matched`` key means "this extension cannot wait" — never "it did
    not match" — and it says so in a code of its OWN.

    ``navigate_tab`` is an OLD command with a NEW parameter: a pre-wave bundle drops
    ``waitUntil`` as an unknown key, does the ``tabs.update`` and answers ``{ok:true}``.
    Reading ``matched`` off that with ``.get`` manufactured ``{matched: false,
    elapsed_ms: 0}`` — a verdict nobody reached, and one the agent cannot tell from an
    honest "the condition never became true". Same class as ``open_tab``'s ``windowId``
    cross-check: new service + old extension is a guaranteed state.

    THE CODE IS THE POINT OF THIS ASSERTION. It used to be ``precondition_failed``, which
    this verb also answers for four ARGUMENT refusals — and the agent's move is opposite
    there ("fix the argument and call again") to here ("this copy's extension is too old;
    retrying cannot help"). §11's rule for ``pinned_cross_window`` is that a distinct
    situation gets a distinct code so a caller can branch without parsing prose.
    """
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    with pytest.raises(tools.ToolError) as ei:
        await _run_with_response(
            lambda: tools.navigate_tab(_app(db, reg), instance="main", tab_id=2,
                                       url="https://a/", wait_until="load",
                                       timeout_ms=5000),
            cs, ws, {"ok": True},  # the pre-wave answer: it never learned to wait
        )
    assert ei.value.code == "extension_too_old"
    assert ei.value.code != protocol.ERR_PRECONDITION_FAILED  # branchable, not prose
    assert "matched" in ei.value.message


async def test_navigate_tab_without_a_wait_keeps_its_pre_wave_answer(tmp_path):
    # The reset path (§8) reads this shape; omitting the parameter must reproduce it.
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.navigate_tab(_app(db, reg), instance="main", tab_id=2,
                                   url="https://a/"),
        cs, ws, {"ok": True},
    )
    assert out == {"ok": True, "result": {"ok": True}}


# --- wake_tab (#68): reload a discarded tab and wait for it to load ----------
async def test_wake_tab_sends_command_and_renames_was_discarded(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    # A real wake: the extension answers `wasDiscarded:true` (camelCase wire), which the tool
    # renames to snake_case `was_discarded` — the same rename wait_for/navigate_tab do.
    out, frame = await _run_with_response(
        lambda: tools.wake_tab(_app(db, reg), instance="main", tab_id=2),
        cs, ws, {"wasDiscarded": True},
    )
    assert frame["command"] == protocol.CMD_WAKE_TAB
    assert frame["params"]["tabId"] == 2
    assert out == {"ok": True, "was_discarded": True}
    # A no-op reload of an already-live tab reports `was_discarded:false`.
    out2, _ = await _run_with_response(
        lambda: tools.wake_tab(_app(db, reg), instance="main", tab_id=2),
        cs, ws, {"wasDiscarded": False},
    )
    assert out2 == {"ok": True, "was_discarded": False}


async def test_wake_tab_carries_the_wait_and_a_longer_socket_budget(tmp_path, monkeypatch):
    # The extension polls `status:complete` inside a deadline; the tool hands it the
    # EXECUTE_JS_MAX_TIMEOUT_MS ceiling and a socket budget that OUTLIVES it, the same ordering
    # rule as wait_for / navigate_tab {waitUntil} — else the command times out on the wire first.
    db = await _make_db(tmp_path)
    app = _app(db, Registry(), _settings(cmd_timeout_ms=1000, execute_js_max_timeout_ms=30000))
    seen = await _capture_budget(monkeypatch, lambda: tools.wake_tab(
        app, instance="main", tab_id=2), {"wasDiscarded": True})
    assert seen["command"] == protocol.CMD_WAKE_TAB
    assert seen["params"] == {"tabId": 2, "timeoutMs": 30000}
    assert seen["cmd_timeout_ms"] > 30000  # the socket outlives the poll deadline


async def test_wake_tab_is_refused_while_stopped_and_sends_nothing(tmp_path):
    # wake_tab reloads the tab — a MUTATION — so the stop switch refuses it, like navigate_tab,
    # and no frame leaves the socket.
    db = await _make_db(tmp_path)
    reg = Registry()
    _cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app)
    with pytest.raises(tools.ToolError) as ei:
        await tools.wake_tab(app, instance="main", tab_id=2)
    assert ei.value.code == "stopped"
    assert ws.sent == []


# --- exemptions: the agent's «не трогать» lease (§10/§11) --------------------
async def _known_instance(db, iid="main"):
    await _insert_instance(db, iid)


async def test_set_list_clear_exemption_round_trip(tmp_path):
    db = await _make_db(tmp_path)
    await _known_instance(db)
    app = _app(db)
    out = await tools.set_exemption(app, instance="main", url="https://a/b?x=1", ttl_s=600)
    assert out["ok"] is True
    ex = out["exemption"]
    assert ex["instance_id"] == "main" and ex["reason"] == "mcp"
    assert ex["until"] > tools._now_ms()

    listed = await tools.list_exemptions(app)
    assert [e["url"] for e in listed["exemptions"]] == ["https://a/b?x=1"]
    # The pass matches on the NORMALIZED url, so the row carries it for the agent to see.
    assert listed["exemptions"][0]["url_norm"] == "https://a/b"

    # A repeat REFRESHES the deadline instead of growing a second row (PK is the pair).
    again = await tools.set_exemption(app, instance="main", url="https://a/b?x=1",
                                      ttl_s=1200, reason="task-42")
    assert again["exemption"]["until"] > ex["until"]
    listed = await tools.list_exemptions(app)
    assert len(listed["exemptions"]) == 1 and listed["exemptions"][0]["reason"] == "task-42"

    assert await tools.clear_exemption(app, instance="main", url="https://a/b?x=1") == {
        "ok": True, "deleted": 1}
    # Idempotent: lifting an already-lifted exemption is not an error.
    assert (await tools.clear_exemption(app, instance="main", url="https://a/b?x=1"))["deleted"] == 0


async def test_set_exemption_honours_the_shared_never_infinite_ceiling(tmp_path):
    # The MCP door must not be able to write a protection the HTTP door refuses: an
    # unbounded row would quietly retire a URL from curation forever (§7).
    db = await _make_db(tmp_path)
    await _known_instance(db)
    app = _app(db)
    now = tools._now_ms()
    out = await tools.set_exemption(app, instance="main", url="https://a/", ttl_s=10**9)
    assert out["exemption"]["until"] <= now + exemptions_api.MAX_MS + 5000
    for bad in (0, -5, "600"):
        with pytest.raises(tools.ToolError):
            await tools.set_exemption(app, instance="main", url="https://a/", ttl_s=bad)


async def test_set_exemption_reuses_the_url_and_instance_guards(tmp_path):
    """Reused, not re-derived: a scheme-less url normalises to something no live tab can
    equal, so the row would be a permanently invisible no-op that LOOKS active."""
    db = await _make_db(tmp_path)
    await _known_instance(db)
    app = _app(db)
    with pytest.raises(tools.ToolError) as ei:
        await tools.set_exemption(app, instance="main", url="grafana.lc/d/1", ttl_s=60)
    assert "http" in ei.value.message
    with pytest.raises(tools.ToolError):
        await tools.set_exemption(app, instance="typo", url="https://a/", ttl_s=60)


async def test_exemption_writes_are_refused_while_stopped(tmp_path):
    db = await _make_db(tmp_path)
    await _known_instance(db)
    app = _app(db)
    await tools.pause(app)
    for call in (
        lambda: tools.set_exemption(app, instance="main", url="https://a/", ttl_s=60),
        lambda: tools.clear_exemption(app, instance="main", url="https://a/"),
    ):
        with pytest.raises(tools.ToolError) as ei:
            await call()
        assert ei.value.code == "stopped"
    # Reading is never gated — an agent must still be able to see what is protected.
    assert (await tools.list_exemptions(app))["exemptions"] == []


async def test_list_exemptions_can_be_scoped_to_one_instance(tmp_path):
    db = await _make_db(tmp_path)
    await _known_instance(db, "main")
    await _known_instance(db, "media")
    app = _app(db)
    await tools.set_exemption(app, instance="main", url="https://a/", ttl_s=60)
    await tools.set_exemption(app, instance="media", url="https://b/", ttl_s=60)
    out = await tools.list_exemptions(app, instance="media")
    assert [e["url"] for e in out["exemptions"]] == ["https://b/"]


# --- open_tab lease ----------------------------------------------------------
async def test_open_tab_lease_writes_an_exemption_the_pass_honours(tmp_path):
    # Without it, a tab the agent opens for a task is fair game for the very next pass.
    db = await _make_db(tmp_path)
    await _known_instance(db)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    out, _frame = await _run_with_response(
        lambda: tools.open_tab(app, instance="main", url="https://task/", lease_ttl_s=900),
        cs, ws, {"tabId": 7, "windowId": 1},
    )
    assert out["ok"] is True and out["lease"]["ok"] is True
    rows = await db.read(lambda c: c.execute(
        "SELECT instance_id, url, reason FROM exemptions").fetchall())
    assert rows == [("main", "https://task/", "mcp_lease")]


async def test_open_tab_without_lease_ttl_writes_nothing_and_reports_nothing(tmp_path):
    db = await _make_db(tmp_path)
    await _known_instance(db)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    out, _frame = await _run_with_response(
        lambda: tools.open_tab(_app(db, reg), instance="main", url="https://task/"),
        cs, ws, {"tabId": 7, "windowId": 1},
    )
    assert out == {"ok": True, "result": {"tabId": 7, "windowId": 1}}  # unchanged shape
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM exemptions").fetchone()) == (0,)


async def test_a_failed_lease_WRITE_is_REPORTED_and_never_fails_the_open(tmp_path):
    """A RUNTIME write fault degrades softly — that half of the contract is right.

    The tab IS open by the time the write runs, so turning the fault into a failed open
    would report "nothing happened" about a tab that exists and invite the agent to open a
    second one. Simulated with a ``db.write`` that raises, which is the real shape of the
    failure (a degraded DB, a full disk) — NOT a bad argument, which is refused earlier and
    never reaches here.
    """
    db = await _make_db(tmp_path)
    await _known_instance(db)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)

    async def _boom(_fn):
        raise sqlite3.OperationalError("disk I/O error")

    app.state.db = SimpleNamespace(read=db.read, write=_boom)
    out, _frame = await _run_with_response(
        lambda: tools.open_tab(app, instance="main", url="https://task/", lease_ttl_s=600),
        cs, ws, {"tabId": 7, "windowId": 1},
    )
    assert out["ok"] is True and out["result"] == {"tabId": 7, "windowId": 1}
    assert out["lease"] == {"ok": False, "error": "lease_write_failed",
                            "message": "disk I/O error"}
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM exemptions").fetchone()) == (0,)


async def test_an_INVALID_lease_argument_is_refused_before_the_tab_is_opened(tmp_path):
    """A bad ``lease_ttl_s`` is a HARD refusal with NO frame sent — not a soft report.

    ``ok: true`` with ``lease: {ok: false}`` for a value ``set_exemption`` refuses outright
    makes the contract depend on which door the agent knocked at. And judging an argument
    never required a tab to exist, so there is nothing to degrade around: refusing early
    also leaves no orphan tab behind.

    The code is asserted to be THE SAME one ``set_exemption`` produces for the identical
    input, which is the actual property under test — a different-but-still-hard code would
    leave the two doors disagreeing, just about a different thing.
    """
    db = await _make_db(tmp_path)
    await _known_instance(db)
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    for bad in (0, -1, "600"):
        with pytest.raises(tools.ToolError) as door_a:
            await tools.set_exemption(_app(db, reg), instance="main", url="https://task/",
                                      ttl_s=bad)
        with pytest.raises(tools.ToolError) as door_b:
            await tools.open_tab(_app(db, reg), instance="main", url="https://task/",
                                 lease_ttl_s=bad)
        assert door_b.value.code == door_a.value.code == "invalid_request"
    # A bad url is refused on the same path, and likewise before the browser is touched.
    with pytest.raises(tools.ToolError) as exc:
        await tools.open_tab(_app(db, reg), instance="main", url="grafana.lc/d/1",
                             lease_ttl_s=600)
    assert exc.value.code == "invalid_request"
    assert ws.sent == []  # NOTHING went on the socket


async def test_open_tab_lease_judges_the_INSTANCE_exactly_as_set_exemption_does(tmp_path):
    """The instance is an ARGUMENT of the lease, and both doors onto ``exemptions`` must
    judge it the same way.

    ``_write_open_lease`` called ``_upsert`` directly, so ``open_tab`` could write a row for
    an instance ``set_exemption`` refuses — a row the pass can never match, since it matches
    on ``instance_id``. The practical risk is small (a live socket implies an active row),
    which argues for the check being cheap, not for it being absent: two doors that disagree
    about who may be written for is how a rule gets quietly weakened on one side.
    """
    db = await _make_db(tmp_path)
    await _known_instance(db, "main")
    reg = Registry()
    _cs, ws = _put_conn(reg, "ghost")  # a socket, yet no row in `instances`
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as door_a:
        await tools.set_exemption(app, instance="ghost", url="https://task/", ttl_s=600)
    with pytest.raises(tools.ToolError) as door_b:
        await tools.open_tab(app, instance="ghost", url="https://task/", lease_ttl_s=600)
    assert door_b.value.code == door_a.value.code
    assert ws.sent == []  # refused BEFORE the tab was opened, like every other lease argument
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM exemptions").fetchone()) == (0,)


# --- the capability report (§11/§12) -----------------------------------------
async def test_list_instances_reports_what_a_copy_declared_in_its_hello(tmp_path):
    """An agent must be able to see what a copy allows BEFORE it calls and fails."""
    from src.db import queries

    db = await _make_db(tmp_path)
    await _insert_instance(db, "main")
    await db.write(lambda c: queries.hello_upsert(
        c, "main", "sess-1", True, tools._now_ms(),
        ext_version="0.4.2",
    ))
    out = await tools.list_instances(_app(db))
    envelope = out["instances"]["main"]
    # The single JS & Debugger gate (it covers execute_js AND the chrome.debugger path);
    # the former separate `allow_debugger` is gone (migration v6).
    assert envelope["allow_execute_js"] is True
    assert "allow_debugger" not in envelope
    assert envelope["ext_version"] == "0.4.2"
