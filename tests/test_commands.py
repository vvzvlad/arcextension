"""Command send/correlate/timeout/error-code infra (§6) + execute_js audit (§12).

These drive ``send_command`` directly against a fake websocket and an in-memory
registry (no TestClient needed): the round-trip, error-code mapping, timeout with
pending-map cleanup, the sessionId STAMP, and the js_audit-before-send rule.
"""

import asyncio

import pytest

from src.db.access import Database
from src.db.settings_store import set_execute_js_enabled
from src.ext import protocol
from src.ext.commands import CommandError, resolve_response, send_command
from src.ext.registry import ConnState, Registry


class FakeWS:
    """Records every frame sent; send is async like the real socket."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_json(self, msg):
        if self.fail:
            raise RuntimeError("socket is gone")
        self.sent.append(msg)


def _registry_with(session_id="sess-1", ws=None):
    reg = Registry()
    ws = ws or FakeWS()
    cs = ConnState(ws=ws, conn_epoch=1, install_uuid="u", session_id=session_id)
    reg.put("i1", cs)
    return reg, cs, ws


async def _until(pred, timeout=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return pred()


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


# --- round-trip: ok ---------------------------------------------------------
async def test_command_round_trip_ok_and_cleanup():
    reg, cs, ws = _registry_with()
    task = asyncio.create_task(
        send_command(
            reg, None, "i1", protocol.CMD_OPEN_TAB, {"url": "https://x"},
            cmd_timeout_ms=5000,
        )
    )
    assert await _until(lambda: ws.sent)
    frame = ws.sent[-1]
    assert frame["type"] == "command"
    assert frame["command"] == "open_tab"
    assert frame["params"] == {"url": "https://x"}
    # A pending future is registered while awaiting.
    assert list(cs.pending_commands) == [frame["id"]]

    resolve_response(
        cs, {"type": "response", "id": frame["id"], "ok": True,
             "result": {"tabId": 7, "windowId": 1}}
    )
    result = await task
    assert result == {"tabId": 7, "windowId": 1}
    # finally-cleanup removed the pending entry.
    assert cs.pending_commands == {}


# --- session STAMP ----------------------------------------------------------
async def test_command_stamps_current_session():
    reg, cs, ws = _registry_with(session_id="sess-42")
    task = asyncio.create_task(
        send_command(reg, None, "i1", protocol.CMD_FOCUS_TAB, {"tabId": 3},
                     cmd_timeout_ms=5000)
    )
    assert await _until(lambda: ws.sent)
    # THE stamp: the command carries the instance's current session_id (§5) — drop
    # the stamp and this assertion reddens.
    assert ws.sent[-1]["sessionId"] == "sess-42"
    resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                          "result": {"ok": True}})
    await task


# --- expected_session STAMP (#47) -------------------------------------------
async def test_command_stamps_expected_session_when_given():
    # #47: when expected_session is passed, the frame carries THAT session, NOT the live
    # conn_state one. A version that always stamps the live session reddens this (it would
    # stamp "sess-live"), which is exactly what would defeat the extension's stale check
    # after a browser restart.
    reg, cs, ws = _registry_with(session_id="sess-live")
    task = asyncio.create_task(
        send_command(reg, None, "i1", protocol.CMD_FOCUS_TAB, {"tabId": 3},
                     cmd_timeout_ms=5000, expected_session="sess-old")
    )
    assert await _until(lambda: ws.sent)
    assert ws.sent[-1]["sessionId"] == "sess-old"  # the PASSED session, not "sess-live"
    resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                          "result": {"ok": True}})
    await task


async def test_command_stamps_live_session_when_expected_session_is_none():
    # The fail-open twin (#47): expected_session=None => stamp the current live session,
    # exactly as before — so a caller that omits it goes unprotected, by design.
    reg, cs, ws = _registry_with(session_id="sess-live")
    task = asyncio.create_task(
        send_command(reg, None, "i1", protocol.CMD_FOCUS_TAB, {"tabId": 3},
                     cmd_timeout_ms=5000, expected_session=None)
    )
    assert await _until(lambda: ws.sent)
    assert ws.sent[-1]["sessionId"] == "sess-live"
    resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                          "result": {"ok": True}})
    await task


# --- stale_session propagated ----------------------------------------------
async def test_stale_session_propagated_as_command_error():
    reg, cs, ws = _registry_with(session_id="sess-1")
    task = asyncio.create_task(
        send_command(reg, None, "i1", protocol.CMD_FOCUS_TAB, {"tabId": 3},
                     cmd_timeout_ms=5000)
    )
    assert await _until(lambda: ws.sent)
    resolve_response(
        cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": False,
             "error": {"code": protocol.ERR_STALE_SESSION, "message": "foreign session"}}
    )
    with pytest.raises(CommandError) as ei:
        await task
    assert ei.value.code == "stale_session"


# --- error code -> CommandError --------------------------------------------
async def test_error_code_becomes_command_error():
    reg, cs, ws = _registry_with()
    task = asyncio.create_task(
        send_command(reg, None, "i1", protocol.CMD_GET_TAB, {"tabId": 9},
                     cmd_timeout_ms=5000)
    )
    assert await _until(lambda: ws.sent)
    resolve_response(
        cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": False,
             "error": {"code": protocol.ERR_NO_SUCH_TAB}}
    )
    with pytest.raises(CommandError) as ei:
        await task
    assert ei.value.code == "no_such_tab"


# --- timeout cleans up the pending map -------------------------------------
async def test_timeout_raises_and_cleans_pending():
    reg, cs, ws = _registry_with()
    with pytest.raises(CommandError) as ei:
        await send_command(reg, None, "i1", protocol.CMD_FOCUS_TAB, {"tabId": 1},
                           cmd_timeout_ms=30)  # never resolved
    assert ei.value.code == "timeout"
    # The pending entry must be gone (finally-cleanup), else the map leaks.
    assert cs.pending_commands == {}


# --- no live connection -----------------------------------------------------
async def test_no_connection_raises():
    reg = Registry()  # empty
    with pytest.raises(CommandError) as ei:
        await send_command(reg, None, "nope", protocol.CMD_FOCUS_TAB, {},
                           cmd_timeout_ms=5000)
    assert ei.value.code == "no_connection"


# --- unknown response id is ignored ----------------------------------------
async def test_unknown_response_id_ignored():
    _, cs, _ = _registry_with()
    # No pending future for this id => resolve_response returns False (ignored).
    assert resolve_response(cs, {"id": "cmd-nope", "ok": True}) is False


# --- send failure -> CommandError + audit outcome recorded ------------------
async def test_send_failure_raises_command_error_and_records_audit(tmp_path):
    # A socket that fails on send (closed between lookup and send) must raise
    # CommandError (the function contract), NOT a raw exception, AND record the
    # execute_js audit outcome — a failed send is still an attempt (§12).
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with(ws=FakeWS(fail=True))
        with pytest.raises(CommandError) as ei:
            await send_command(
                reg, db, "i1", protocol.CMD_EXECUTE_JS,
                {"code": "x", "world": "MAIN"}, cmd_timeout_ms=5000,
            )
        assert ei.value.code == "no_connection"
        assert cs.pending_commands == {}            # no leaked pending future
        rows = await _read_audit(db)
        assert len(rows) == 1
        assert rows[0][6] == "error"                # outcome recorded despite failed send
    finally:
        await db.close()


# --- execute_js fail-closed without an audit sink ---------------------------
async def test_execute_js_without_db_refuses_and_sends_no_frame():
    # execute_js MUST NOT be sent when there is no db to write the js_audit row
    # (§12): fail-closed with `internal`, and NOTHING reaches the socket — the
    # "JS ran without an audit row" path must not exist.
    reg, cs, ws = _registry_with()
    with pytest.raises(CommandError) as ei:
        await send_command(
            reg, None, "i1", protocol.CMD_EXECUTE_JS,
            {"code": "danger()", "world": "MAIN"}, cmd_timeout_ms=5000,
        )
    assert ei.value.code == "internal"
    assert ws.sent == []            # no command frame was sent
    assert cs.pending_commands == {}


# --- execute_js writes js_audit BEFORE the send ----------------------------
async def _read_audit(db):
    return await db.read(
        lambda c: c.execute(
            "SELECT instance_id, tab_id, world, code, initiator, auth_ctx, outcome "
            "FROM js_audit"
        ).fetchall()
    )


async def test_execute_js_audit_written_before_send_then_outcome_ok(tmp_path):
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with()
        task = asyncio.create_task(
            send_command(
                reg, db, "i1", protocol.CMD_EXECUTE_JS,
                {"code": "return 1+1", "world": "MAIN", "tabId": 5},
                cmd_timeout_ms=5000, initiator="mcp", auth_ctx="mcp:sess-abc",
            )
        )
        # The row exists BEFORE we reply (outcome still NULL) — proof it was
        # written before the send, so a rejected/timed-out call is also recorded.
        assert await _until(lambda: ws.sent)
        rows = await _read_audit(db)
        assert len(rows) == 1
        assert rows[0] == ("i1", 5, "MAIN", "return 1+1", "mcp", "mcp:sess-abc", None)

        resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                              "result": {"results": [2]}})
        await task
        rows = await _read_audit(db)
        assert rows[0][6] == "ok"  # outcome updated after
    finally:
        await db.close()


async def test_execute_js_audit_records_HOW_the_code_ran(tmp_path):
    """``await_promise`` is audited, because ``code`` alone does not say what it meant.

    The same text is a different program on the two paths: as an async-function body it
    has its own scope and ``return`` is how it answers, while under indirect eval it runs
    in the page's globals and a top-level ``return`` is a SyntaxError. §12 keeps the code
    in FULL because "усечённый код нереконструируем" — a row that cannot say which of the
    two produced its effect is unreconstructable for exactly the same reason.
    """
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with()
        for params, expected in (
            ({"code": "1+1", "tabId": 5}, 0),                          # omitted => eval path
            ({"code": "return 1", "tabId": 5, "awaitPromise": True}, 1),
        ):
            before = len(ws.sent)  # ws.sent accumulates: wait for a NEW frame, not any frame
            task = asyncio.create_task(send_command(
                reg, db, "i1", protocol.CMD_EXECUTE_JS, params,
                cmd_timeout_ms=5000, initiator="mcp", auth_ctx="mcp:s",
            ))
            assert await _until(lambda: len(ws.sent) > before)
            resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                                  "result": {}})
            await task
        modes = await db.read(lambda c: c.execute(
            "SELECT await_promise FROM js_audit ORDER BY id").fetchall())
        assert modes == [(0,), (1,)]
    finally:
        await db.close()


async def test_execute_js_audit_records_timeout(tmp_path):
    # A timed-out execute_js (no response ever) STILL leaves an audit row — the
    # before-send write is what guarantees the only trace of code execution.
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with()
        with pytest.raises(CommandError) as ei:
            await send_command(
                reg, db, "i1", protocol.CMD_EXECUTE_JS,
                {"code": "danger()", "world": "ISOLATED"},
                cmd_timeout_ms=30,
            )
        assert ei.value.code == "timeout"
        rows = await _read_audit(db)
        assert len(rows) == 1
        assert rows[0][3] == "danger()"      # full code preserved
        assert rows[0][6] == "error"         # outcome marked on timeout
    finally:
        await db.close()


async def test_execute_js_disabled_outcome(tmp_path):
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with()
        task = asyncio.create_task(
            send_command(reg, db, "i1", protocol.CMD_EXECUTE_JS,
                         {"code": "x", "world": "MAIN"}, cmd_timeout_ms=5000)
        )
        assert await _until(lambda: ws.sent)
        resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": False,
                              "error": {"code": protocol.ERR_JS_DISABLED}})
        with pytest.raises(CommandError):
            await task
        rows = await _read_audit(db)
        assert rows[0][6] == "disabled"
    finally:
        await db.close()


# --- runtime kill-switch (§12): refuse execute_js before it is ever sent -----
async def test_execute_js_refused_by_kill_switch_sends_no_frame(tmp_path):
    # With the runtime switch OFF the service must REFUSE an execute_js: no frame
    # is put on the socket, and js_audit records outcome='disabled' (reason
    # 'kill_switch'). Remove the gate and a frame would be sent + this reddens.
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: set_execute_js_enabled(c, False))
        reg, cs, ws = _registry_with()
        with pytest.raises(CommandError) as ei:
            await send_command(
                reg, db, "i1", protocol.CMD_EXECUTE_JS,
                {"code": "danger()", "world": "MAIN", "tabId": 5},
                cmd_timeout_ms=5000,
            )
        assert ei.value.code == protocol.ERR_JS_DISABLED
        # THE proof it was blocked at the service edge: nothing was sent, and no
        # pending future leaked (we never reached the send/await).
        assert ws.sent == []
        assert cs.pending_commands == {}
        # The attempt is still the only trace of an execute_js (§12): full code +
        # outcome='disabled' + reason 'kill_switch'.
        rows = await db.read(
            lambda c: c.execute(
                "SELECT code, outcome, detail FROM js_audit"
            ).fetchall()
        )
        assert len(rows) == 1
        assert rows[0] == ("danger()", "disabled", "kill_switch")
    finally:
        await db.close()


async def test_execute_js_sent_when_kill_switch_on_by_default(tmp_path):
    # Default (no settings row) => the switch is ON => the frame IS sent.
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with()
        task = asyncio.create_task(
            send_command(reg, db, "i1", protocol.CMD_EXECUTE_JS,
                         {"code": "x", "world": "MAIN"}, cmd_timeout_ms=5000)
        )
        # A frame reaches the socket precisely because the switch defaults ON.
        assert await _until(lambda: ws.sent)
        assert ws.sent[-1]["command"] == "execute_js"
        resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                              "result": {"results": [1]}})
        assert await task == {"results": [1]}
        rows = await _read_audit(db)
        assert rows[0][6] == "ok"
    finally:
        await db.close()


# --- start_js: the SAME arbitrary-code gate as execute_js (§12) --------------
# start_js carries arbitrary caller code fire-and-forget, so it rides the identical
# audit-before-send + kill-switch + db-None-fail-closed path. These pin that the branch was
# widened to CMD_START_JS and NOT to the fixed verbs (poll_job below).
async def test_start_js_audit_written_before_send_then_outcome_ok(tmp_path):
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with()
        task = asyncio.create_task(
            send_command(
                reg, db, "i1", protocol.CMD_START_JS,
                {"code": "await slowScrape()", "world": "MAIN", "tabId": 8,
                 "jobId": "job-abc"},
                cmd_timeout_ms=5000, initiator="mcp", auth_ctx="mcp:sess-j",
            )
        )
        # The row exists BEFORE we reply (outcome still NULL) — proof it was written before
        # the send, exactly like execute_js: a rejected/timed-out start_js is still recorded.
        assert await _until(lambda: ws.sent)
        assert ws.sent[-1]["command"] == "start_js"
        rows = await _read_audit(db)
        assert len(rows) == 1
        assert rows[0] == ("i1", 8, "MAIN", "await slowScrape()", "mcp", "mcp:sess-j", None)

        resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                              "result": {"jobId": "job-abc"}})
        await task
        rows = await _read_audit(db)
        assert rows[0][6] == "ok"  # outcome updated after
    finally:
        await db.close()


async def test_start_js_without_db_refuses_and_sends_no_frame():
    # start_js MUST NOT be sent when there is no db to write the js_audit row (§12):
    # fail-closed with `internal`, NOTHING reaches the socket. Fire-and-forget makes the
    # trace matter MORE, not less — the code keeps running after the frame is answered.
    reg, cs, ws = _registry_with()
    with pytest.raises(CommandError) as ei:
        await send_command(
            reg, None, "i1", protocol.CMD_START_JS,
            {"code": "danger()", "world": "MAIN", "jobId": "job-x"}, cmd_timeout_ms=5000,
        )
    assert ei.value.code == "internal"
    assert ws.sent == []
    assert cs.pending_commands == {}


async def test_start_js_refused_by_kill_switch_sends_no_frame(tmp_path):
    # The runtime kill-switch gates start_js exactly as execute_js: switch OFF => no frame,
    # and js_audit records outcome='disabled' (reason 'kill_switch').
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: set_execute_js_enabled(c, False))
        reg, cs, ws = _registry_with()
        with pytest.raises(CommandError) as ei:
            await send_command(
                reg, db, "i1", protocol.CMD_START_JS,
                {"code": "danger()", "world": "MAIN", "tabId": 5, "jobId": "job-y"},
                cmd_timeout_ms=5000,
            )
        assert ei.value.code == protocol.ERR_JS_DISABLED
        assert ws.sent == []
        assert cs.pending_commands == {}
        rows = await db.read(
            lambda c: c.execute("SELECT code, outcome, detail FROM js_audit").fetchall()
        )
        assert rows == [("danger()", "disabled", "kill_switch")]
    finally:
        await db.close()


# --- poll_job / scroll_until: FIXED verbs, NEVER audited ---------------------
async def test_poll_job_is_not_audited_and_switch_is_ignored(tmp_path):
    # poll_job reads a page global with a fixed function — no arbitrary code — so it must
    # write NO js_audit row and must NOT be touched by the kill-switch, even with the switch
    # OFF (which would refuse an arbitrary-code verb before the send).
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: set_execute_js_enabled(c, False))
        reg, cs, ws = _registry_with()
        task = asyncio.create_task(
            send_command(reg, db, "i1", protocol.CMD_POLL_JOB,
                         {"tabId": 5, "jobId": "job-z"}, cmd_timeout_ms=5000)
        )
        # It reaches the socket despite the switch being OFF — the switch does not gate it.
        assert await _until(lambda: ws.sent)
        assert ws.sent[-1]["command"] == "poll_job"
        resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                              "result": {"state": "running"}})
        assert await task == {"state": "running"}
        rows = await _read_audit(db)
        assert rows == []  # NO audit row for a fixed verb
    finally:
        await db.close()


async def test_scroll_until_is_not_audited(tmp_path):
    # scroll_until is a fixed-function verb too — selectors + a direction, no code — so it
    # writes no js_audit row.
    db = await _make_db(tmp_path)
    try:
        reg, cs, ws = _registry_with()
        task = asyncio.create_task(
            send_command(reg, db, "i1", protocol.CMD_SCROLL_UNTIL,
                         {"tabId": 5, "countSelector": ".item", "direction": "down",
                          "timeoutMs": 5000}, cmd_timeout_ms=9000)
        )
        assert await _until(lambda: ws.sent)
        assert ws.sent[-1]["command"] == "scroll_until"
        resolve_response(cs, {"type": "response", "id": ws.sent[-1]["id"], "ok": True,
                              "result": {"count": 40, "rounds": 3, "stopped": "stable",
                                         "elapsedMs": 1400}})
        await task
        rows = await _read_audit(db)
        assert rows == []
    finally:
        await db.close()
