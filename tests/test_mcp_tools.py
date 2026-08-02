"""MCP tool handlers (§11), unit-tested directly against the reused logic.

Covers each tool's handler plus the guards the reviewer mutation-checks:
* confirm_impact on an MCP rule write,
* execute_js audited (initiator='mcp' + the MCP session as auth_ctx) and kill-switch,
* relocate_tab writing a live ``relocate`` row (initiator='mcp'),
* a paused system refusing a mutating verb (and no command leaving the socket).
"""

import asyncio
from types import SimpleNamespace

import pytest

from src.curator import lease
from src.curator import pause as pause_ops
from src.db.access import Database
from src.db.audit import insert_js_audit  # noqa: F401  (schema presence)
from src.db.settings_store import get_setting, set_execute_js_enabled
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
    s = dict(
        idle_minutes=60, pass_interval_min=5, cmd_timeout_ms=1000,
        snapshot_timeout_ms=200, lease_ttl_ms=600_000, state_fresh_ms=3000,
        quarantine_ttl_min=1440, main_instance_id="main", pause_default_min=60,
    )
    s.update(over)
    return SimpleNamespace(**s)


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


async def _insert_instance(db, iid, *, session_id=None, snapshot_at=None, connected=0):
    def _w(c):
        c.execute(
            "INSERT INTO instances (id, connected, session_id, snapshot_at) "
            "VALUES (?, ?, ?, ?)",
            (iid, connected, session_id, snapshot_at),
        )
    await db.write(_w)


async def _insert_tab(db, iid, tab_id, *, url, title="t", opened_at=1000,
                      last_active_at=1000, age_unknown=0, now=2000):
    def _w(c):
        c.execute(
            "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, opened_at, "
            "last_active_at, age_unknown, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (iid, tab_id, 1, url, title, opened_at, last_active_at, age_unknown, now),
        )
    await db.write(_w)


# --- reads -------------------------------------------------------------------
async def test_list_instances_returns_snapshot_at_and_paused_until(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", snapshot_at=1234, connected=0)
    app = _app(db)
    out = await tools.list_instances(app)
    assert out["paused_until"] is None
    ids = {i["id"]: i for i in out["instances"]}
    assert ids["main"]["snapshot_at"] == 1234

    # A paused curator surfaces paused_until so an agent does not read pause as a break.
    now = tools._now_ms()
    await db.write(lambda c: pause_ops.pause(c, now=now, minutes=30))
    out2 = await tools.list_instances(app)
    assert out2["paused_until"] is not None and out2["paused_until"] > now


async def test_list_tabs_returns_tabs_and_per_instance_snapshot_at(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "main", snapshot_at=555)
    await _insert_tab(db, "main", 1, url="https://a")
    out = await tools.list_tabs(_app(db))
    assert [t["tab_id"] for t in out["tabs"]] == [1]
    assert out["snapshot_at"] == {"main": 555}


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


async def test_reset_singleton_returns_canonical_url(tmp_path):
    db = await _make_db(tmp_path)
    from src.rules import access as ra
    rid = await db.write(lambda c: ra.insert_rule(
        c, pattern="x.com", instance_id="main", canonical_url="https://x.com/home", created_at=1))
    out = await tools.reset_singleton(_app(db), rule_id=rid)
    assert out["ok"] is True and out["canonical_url"] == "https://x.com/home"


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


# --- relocate_tab: phase-A open + live relocate row (initiator='mcp') --------
async def test_relocate_tab_writes_relocate_action_initiator_mcp(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "themed", session_id="s-themed")
    await _insert_instance(db, "main", session_id="s-main-db")
    await _insert_tab(db, "themed", 5, url="https://grafana/dash", opened_at=100, last_active_at=200)
    reg = Registry()
    cs_to, ws = _put_conn(reg, "main", session_id="s-main-live")
    app = _app(db, reg)

    out, frame = await _run_with_response(
        lambda: tools.relocate_tab(app, instance_from="themed", tab_id=5, instance_to="main",
                                   auth_ctx="mcp-s"),
        cs_to, ws, {"tabId": 99, "windowId": 1},
    )
    assert out["ok"] is True and out["tab_id_to"] == 99
    assert frame["command"] == protocol.CMD_OPEN_TAB  # phase A opened the copy in target

    row = await db.read(lambda c: c.execute(
        "SELECT kind, status, initiator, instance_from, instance_to, tab_id, tab_id_to, "
        "session_id_from, session_id_to, url FROM actions"
    ).fetchone())
    assert row == ("relocate", "done", "mcp", "themed", "main", 5, 99,
                   "s-themed", "s-main-live", "https://grafana/dash")
    # The copy's mirror row exists in the target (so the pass's phase B can verify it).
    copy = await db.read(lambda c: c.execute(
        "SELECT url FROM tabs WHERE instance_id='main' AND tab_id=99").fetchone())
    assert copy == ("https://grafana/dash",)


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


async def test_pause_refuses_mutating_verb_and_sends_nothing(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    _, ws = _put_conn(reg, "main")
    app = _app(db, reg)
    await tools.pause(app, minutes=30)  # arm the pause

    with pytest.raises(tools.ToolError) as ei:
        await tools.open_tab(app, instance="main", url="https://a", auth_ctx="s")
    assert ei.value.code == "paused"
    assert ws.sent == []  # the mutating verb never reached the socket

    # A read is NOT gated by pause.
    assert "instances" in await tools.list_instances(app)
    # After resume the same verb proceeds.
    await tools.resume(app)
    task = asyncio.create_task(tools.open_tab(app, instance="main", url="https://a", auth_ctx="s"))
    for _ in range(400):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert ws.sent, "verb should proceed after resume"
    resolve_response(reg.get("main"),
                     {"type": "response", "id": ws.sent[-1]["id"], "ok": True, "result": {"tabId": 1}})
    assert (await task)["ok"] is True


# --- run_pass ----------------------------------------------------------------
async def test_run_pass_dry_run_writes_nothing(tmp_path):
    db = await _make_db(tmp_path)
    out = await tools.run_pass(_app(db), dry_run=True)
    assert out["status"] == "dry_run"
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM actions").fetchone()) == (0,)
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM passes").fetchone()) == (0,)


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
