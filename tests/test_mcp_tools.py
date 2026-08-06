"""MCP tool handlers (§11), unit-tested directly against the reused logic.

Covers each tool's handler plus the guards the reviewer mutation-checks:
* confirm_impact on an MCP rule write,
* execute_js audited (initiator='mcp' + the MCP session as auth_ctx) and kill-switch,
* relocate_tab writing a live ``relocate`` row (initiator='mcp'),
* a paused system refusing a mutating verb (and no command leaving the socket).
"""

import asyncio
import sqlite3
from conftest import make_settings
from types import SimpleNamespace

import pytest

from src.curator import lease
from src.curator import pause as pause_ops
from src.db import state as state_read
from src.db.access import Database
from src.db.audit import insert_js_audit  # noqa: F401  (schema presence)
from src.db.settings_store import get_setting, set_execute_js_enabled, set_setting
from src.ext import protocol
from src.ext.commands import resolve_response
from src.ext.registry import ConnState, Registry
from src.mcpiface import tools


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
async def test_list_instances_returns_freshness_envelope_and_paused_until(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", snapshot_at=1234, connected=0)
    app = _app(db)
    out = await tools.list_instances(app)
    assert out["paused_until"] is None
    # `instances` is now a per-instance freshness envelope keyed by id (§6/§11), not a
    # list of mirror rows. No live socket for "main" => ensure_fresh reports disconnected.
    main = out["instances"]["main"]
    # session_id rides the envelope (#47); "main" was inserted with no session_id.
    assert main == {"snapshot_at": 1234, "fresh": False, "reason": "disconnected",
                    "session_id": None}

    # A paused curator surfaces paused_until so an agent does not read pause as a break.
    now = tools._now_ms()
    await db.write(lambda c: pause_ops.pause(c, now=now, minutes=30))
    out2 = await tools.list_instances(app)
    assert out2["paused_until"] is not None and out2["paused_until"] > now


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
        "main": {"snapshot_at": now, "fresh": True, "reason": "fresh", "session_id": "sess-1"}
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
                            "session_id": "sess-1"}
    assert inst["media"] == {"snapshot_at": None, "fresh": False, "reason": "disconnected",
                             "session_id": "sess-2"}
    # The disconnected sibling did not drop the fresh instance's tab.
    assert [t["tab_id"] for t in out["tabs"]] == [1]


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
                           "session_id": "sess-2"}
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


async def test_move_tab_is_refused_while_paused_and_sends_nothing(tmp_path):
    # A move is automation like every other mutating verb (§7/§12), and the MCP door has
    # no `force`: an agent is not a human at the keyboard.
    db = await _make_db(tmp_path)
    reg = Registry()
    _cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app, minutes=30)
    with pytest.raises(tools.ToolError) as ei:
        await tools.move_tab(app, instance="main", tab_id=7, window_id=3)
    assert ei.value.code == "paused"
    assert ws.sent == []


# --- execute_js: audited (§12) + kill-switch --------------------------------
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


async def test_execute_js_refused_by_kill_switch_still_audited_and_not_sent(tmp_path):
    db = await _make_db(tmp_path)
    await db.write(lambda c: set_execute_js_enabled(c, False))
    reg = Registry()
    cs, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.execute_js(app, instance="main", tab_id=2, code="evil()", auth_ctx="s")
    assert ei.value.code == protocol.ERR_JS_DISABLED
    assert ws.sent == []  # refused before any frame reached the socket
    outcome = await db.read(lambda c: c.execute("SELECT outcome, initiator FROM js_audit").fetchone())
    assert outcome == ("disabled", "mcp")


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
    # Acceptance 11: the verb refuses while the stop switch (pause) is armed and sends no
    # frames. (The codebase's stop switch is the pause gate; its code is "paused".)
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash")
    now = tools._now_ms()
    await db.write(lambda c: pause_ops.pause(c, now=now, minutes=10))
    reg = Registry()
    _cs_from, ws_from = _put_conn(reg, "themed", session_id="s-themed")
    _cs_to, ws_to = _put_conn(reg, "main", session_id="s-main")
    app = _app(db, reg)
    with pytest.raises(tools.ToolError) as ei:
        await tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main")
    assert ei.value.code == "paused"
    assert ws_from.sent == [] and ws_to.sent == []  # no frame reached any socket
    await _no_new_rows(db)


# --- pause / resume ----------------------------------------------------------
async def test_pause_writes_setting_and_bumps_epoch(tmp_path):
    db = await _make_db(tmp_path)
    app = _app(db)
    before = await db.read(lease.read_lease)
    out = await tools.pause(app, minutes=10)
    assert out["ok"] is True and out["paused_until"] is not None
    after = await db.read(lease.read_lease)
    assert after["epoch"] == before["epoch"] + 1          # bump_epoch stopped any pass
    assert await db.read(lambda c: get_setting(c, "pause_until")) == str(out["paused_until"])

    await tools.resume(app)
    assert await db.read(pause_ops.read_pause_until) is None


async def test_mcp_resume_runs_a_pass_like_the_http_twin(tmp_path):
    """§7/§11 parity: a manual resume runs a pass IMMEDIATELY, whichever door it came
    through. The MCP tool used to stop after the settings write, so an agent's resume
    left the curator idle until the next tick — the same verb with two behaviours.
    Reddens (no `pass` key, no `passes` row) if the tool stops writing settings only."""
    db = await _make_db(tmp_path)
    app = _app(db)
    await tools.pause(app, minutes=30)

    out = await tools.resume(app)
    assert out["ok"] is True
    assert "ttl_shift_ms" in out
    # A REAL pass ran (no instances are connected here → no_ready_instances) and left
    # its row in `passes`, exactly like DELETE /api/pause does.
    assert out["pass"]["status"] == "no_ready_instances"
    assert await db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM passes").fetchone()
    ) == (1,)


async def test_mcp_resume_escapes_a_continuity_break_latch(tmp_path):
    """§7/§11 parity, the other half: the agent's resume must be able to LEAVE
    ``resume_pending``, not only lift a pause.

    The tool is the same door as ``DELETE /api/pause`` (both run
    :func:`src.api.pause.resume_now`), and the latch is armed by a CONTINUITY BREAK as
    well as by an expired pause. On a break there is nothing to clear but the stale
    fingerprint, which only a real pass refreshes — the pass the latch blocks. An agent
    that hit that state had no verb that could leave it: the MCP ``run_pass`` tool takes
    only ``dry_run``, so ``resume`` is its ONLY exit. Reddens if the tool grows a private
    resume path, or if ``resume_now`` stops confirming the latch: the answer is another
    ``resume_pending`` and the curator stays latched forever.
    """
    from src.curator import clock as clockmod

    db = await _make_db(tmp_path)
    app = _app(db)
    # A fleet row (a pass stores a fingerprint only when it saw a fleet, §7) plus a
    # fingerprint taken at a DIFFERENT IDLE_MINUTES than the running config: a §7
    # continuity break with no pause anywhere near it.
    await db.write(lambda c: c.execute(
        "INSERT INTO instances (id, status, connected, conn_epoch) "
        "VALUES ('main', 'active', 0, 0)"))
    fp = await db.read(lambda c: clockmod.current_fingerprint(
        c, idle_minutes=30, main_instance_id="main"))
    await db.write(lambda c: clockmod.store_fingerprint(c, fp))

    # The scheduled pass defers behind a click and arms the latch.
    assert (await tools.run_pass(app))["status"] == "resume_pending"
    assert (await tools.list_instances(app))["resume_pending"] is True

    out = await tools.resume(app)
    assert out["ok"] is True
    # A REAL pass ran (nothing is connected → no_ready_instances), the latch is gone and
    # the fingerprint now matches the running config, so the next tick stays quiet.
    assert out["pass"]["status"] == "no_ready_instances", out["pass"]
    assert (await tools.list_instances(app))["resume_pending"] is False
    assert (await db.read(clockmod.read_stored_fingerprint))["idle_minutes"] == 60
    assert (await tools.run_pass(app))["status"] == "no_ready_instances"


async def test_list_instances_exposes_resume_pending(tmp_path):
    """§7 verbatim: «`resume_pending` виден в `StateResponse` и в `list_instances`».

    Without it an agent sees `paused_until` in the past and no passes happening and
    concludes the curator is broken, when it is deliberately waiting for a click."""
    db = await _make_db(tmp_path)
    app = _app(db)
    assert (await tools.list_instances(app))["resume_pending"] is False

    from src.db.settings_store import set_setting
    await db.write(lambda c: set_setting(
        c, pause_ops.RESUME_PENDING_KEY, '{"since": 1, "plan": {"closures": 3}}'
    ))
    assert (await tools.list_instances(app))["resume_pending"] is True

    # Cleared with the pause (resume clears the latch) → flag drops again.
    await db.write(lambda c: pause_ops.resume(c, now=tools._now_ms()))
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
    await tools.pause(app, minutes=30)  # arm the pause

    with pytest.raises(tools.ToolError) as ei:
        await tools.open_tab(app, instance="main", url="https://a", auth_ctx="s")
    assert ei.value.code == "paused"
    assert ws.sent == []  # the mutating verb never reached the socket

    # A read is NOT gated by pause.
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
