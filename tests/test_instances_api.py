"""``POST /api/instances/:id/merge_windows`` (§9, §10) — the manual «слить окна сейчас».

§9 keeps the button next to the automatic step-9 merge («для случая, когда ждать час не
хочется»); §10's table pins the answer to ``{merged}``, "immediately". The MCP tool
existed and the HTTP twin did not, so the startpage button had nothing to call.

The websocket plays the extension: it receives the ``merge_windows`` command and answers
with the count of tabs it moved.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from conftest import (
    _recv,
    approve_instance,
    instance_headers,
    make_settings,
    secret_hash_for,
)
from starlette.testclient import TestClient

from src.app import create_app

ADMIN_TOKEN = "test-admin-token"
# /api/* accepts either an admin (ADMIN_TOKEN) or an active-instance secret (issue #35 §4).
# The generic tests here just need a valid caller, so they use the admin credential;
# the force/pause tests that must EXECUTE a forced verb switch to an instance secret.
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
    return make_settings(tmp_path, **{**{
            "cmd_timeout_ms": 1500,
            "snapshot_timeout_ms": 500,
        }, **over})


def _q_one(db_path, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _connect(client, instance_id="prox", session="sess-1", db_path=None):
    # Secret-based hello (issue #35): approve the instance (Task E) before it can hello.
    approve_instance(db_path, instance_id)
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json({
        "type": "hello", "protocolVersion": 1,
        "secretHash": secret_hash_for(instance_id),
        "instanceId": instance_id, "installUuid": f"u-{instance_id}",
        "origin": "chrome-extension://abc", "title": "T",
        "sessionId": session, "allowExecuteJs": False,
    })
    _recv(ws)          # hello_ack
    req = _recv(ws)    # initial snapshot_request
    ws.send_json({
        "type": "snapshot", "id": req["id"], "sessionId": session,
        "focusedWindowId": 1, "tabs": [],
        "windows": [{"id": 1, "type": "normal", "state": "normal"}],
    })
    # Wait for the snapshot to actually land instead of sleeping a hopeful 50ms: the
    # channel clears ``pending_snapshot_id`` before it writes, so a set ``snapshot_at``
    # proves both that the mirror is current and that the request slot is free.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        row = _q_one(db_path, "SELECT snapshot_at FROM instances WHERE id=?", (instance_id,))
        if row is not None and row[0] is not None:
            return ws
        time.sleep(0.01)
    raise AssertionError(f"instance {instance_id!r} never applied its initial snapshot")


def test_merge_windows_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/api/instances/prox/merge_windows").status_code == 401
        client.app.state.degraded = True
        assert client.post(
            "/api/instances/prox/merge_windows", headers=AUTH
        ).status_code == 503


def test_merge_windows_returns_the_merged_count(tmp_path):
    # THE contract (§10): 200 {"merged": <int>} — nothing wrapped, nothing else needed
    # by the startpage button.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        ws = _connect(client, db_path=str(tmp_path / "curator.db"))
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post("/api/instances/prox/merge_windows", headers=AUTH)
            )
            cmd = _recv(ws)
            assert cmd["type"] == "command" and cmd["command"] == "merge_windows"
            assert cmd["sessionId"] == "sess-1"      # §5 session stamped
            assert cmd["params"] == {}               # empty = §9's "merge all"
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"merged": 3}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            assert resp.json() == {"merged": 3}
        finally:
            ws.__exit__(None, None, None)


def test_merge_windows_surfaces_command_errors(tmp_path):
    # busy_dragging (the human is holding a tab) is not a server fault: §9 says it is
    # retried, so the client gets a 409 + refetch, not a 502.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        ws = _connect(client, db_path=str(tmp_path / "curator.db"))
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post("/api/instances/prox/merge_windows", headers=AUTH)
            )
            cmd = _recv(ws)
            ws.send_json({
                "type": "response", "id": cmd["id"], "ok": False,
                "error": {"code": "busy_dragging", "message": "held"},
            })
            resp = fut.result(timeout=5)
            assert resp.status_code == 409
            assert resp.json()["error"] == "busy_dragging"
        finally:
            ws.__exit__(None, None, None)


def test_merge_windows_without_a_socket_is_502(tmp_path):
    # No live connection => never a silent success (the §6 no_connection code).
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        resp = client.post("/api/instances/ghost/merge_windows", headers=AUTH)
        assert resp.status_code == 502
        assert resp.json()["error"] == "no_connection"


def test_http_and_mcp_share_one_implementation(tmp_path):
    # §9 keeps BOTH the button and the tool; they must not be two implementations.
    # Reddens if the MCP tool goes back to its own send_command copy.
    from src.api import instances as instances_api
    from src.mcpiface import tools

    calls = []

    async def _fake_core(app, instance_id, params=None, *, initiator="user",
                         auth_ctx=None, forced=False):
        calls.append((instance_id, params, initiator, auth_ctx, forced))
        return {"merged": 7}

    original = instances_api.merge_windows
    instances_api.merge_windows = _fake_core
    try:
        app = create_app(_settings(tmp_path))
        db_path = str(tmp_path / "curator.db")
        with TestClient(app) as client:
            # Authenticate the HTTP call as instance 'prox' so its initiator is 'user'
            # (the human), the value this test contrasts with the MCP tool's 'mcp'.
            approve_instance(db_path, "prox")
            assert client.post(
                "/api/instances/prox/merge_windows",
                headers=instance_headers(secret_hash_for("prox")),
            ).json() == {"merged": 7}

            import asyncio
            out = asyncio.run(
                tools.merge_windows(client.app, instance="prox", auth_ctx="mcp-1")
            )
            assert out["merged"] == 7
    finally:
        instances_api.merge_windows = original

    assert [c[0] for c in calls] == ["prox", "prox"]
    assert calls[0][2] == "user" and calls[1][2] == "mcp"   # initiator is the only diff
    # The MCP path never passes `forced` — it has no force at all (§7/§12).
    assert calls[1][4] is False


# --- §7 pause: the button is forcible, the MCP tool is not ------------------
def _set_setting(db_path, key, value):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _actions(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn.execute(
            "SELECT kind, status, initiator, instance_from, pass_id, detail FROM actions"
        ).fetchall()
    finally:
        conn.close()


def test_merge_windows_is_gated_by_pause_without_force(tmp_path):
    # §7: the merge is automation, so a plain click during a pause is refused — the
    # force exception must stay EXPLICIT, never implied by the verb.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect(client, db_path=str(tmp_path / "curator.db"))
        try:
            _set_setting(db_path, "pause_until", str(int(time.time() * 1000) + 3_600_000))
            resp = client.post("/api/instances/prox/merge_windows", headers=AUTH)
            assert resp.status_code == 423
            assert resp.json()["error"] == "paused"
            resp2 = client.post(
                "/api/instances/prox/merge_windows", headers=AUTH, json={}
            )
            assert resp2.status_code == 423
            assert _actions(db_path) == []      # nothing happened, nothing journaled
        finally:
            ws.__exit__(None, None, None)


def test_merge_windows_force_crosses_the_pause_and_is_journaled(tmp_path):
    """§9 calls this the human's button on the startpage and §7 exempts the human's own
    buttons with an explicit ``force:true``. The forced merge must run AND be tellable
    apart in the archive — via the existing ``detail`` column, no new one (§7).

    Reddens if ``force`` is dropped from the endpoint (423, no command) or if the
    journal row / its marker is dropped."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect(client, db_path=str(tmp_path / "curator.db"))
        try:
            _set_setting(db_path, "pause_until", str(int(time.time() * 1000) + 3_600_000))
            pool = ThreadPoolExecutor(1)
            # force is honoured only for the INSTANCE caller (the human at the §9 button,
            # §35 §4) — authenticate as prox with its secretHash, not as admin.
            fut = pool.submit(lambda: client.post(
                "/api/instances/prox/merge_windows",
                headers=instance_headers(secret_hash_for("prox")),
                json={"force": True},
            ))
            cmd = _recv(ws)                 # the pause did NOT stop it
            assert cmd["command"] == "merge_windows"
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"merged": 2}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200 and resp.json() == {"merged": 2}

            rows = _actions(db_path)
            assert len(rows) == 1
            kind, status, initiator, instance_from, pass_id, detail = rows[0]
            # §7: written as initiator=user, and the force is visible in `detail`.
            assert (kind, status, initiator) == ("window_merge", "done", "user")
            assert instance_from == "prox" and pass_id is None
            payload = json.loads(detail)
            assert payload == {"manual": True, "merged": 2, "force": True}
        finally:
            ws.__exit__(None, None, None)


def test_admin_authenticated_merge_writes_initiator_admin(tmp_path):
    """§35 §5: an ``/api/*`` merge authenticated by ADMIN_TOKEN journals
    ``initiator='admin'`` — distinct from the instance caller's 'user' (the force test
    above) and the MCP tool's 'mcp' (the share test). Reddens if the endpoint stops
    deriving initiator from the caller kind."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect(client, db_path=db_path)  # instance 'prox' active + connected
        try:
            pool = ThreadPoolExecutor(1)
            # AUTH is the admin credential; admin may merge any instance.
            fut = pool.submit(
                lambda: client.post("/api/instances/prox/merge_windows", headers=AUTH)
            )
            cmd = _recv(ws)
            assert cmd["command"] == "merge_windows"
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"merged": 1}})
            assert fut.result(timeout=5).status_code == 200
            rows = _actions(db_path)
            assert len(rows) == 1
            kind, status, initiator, instance_from, pass_id, detail = rows[0]
            assert (kind, status, initiator) == ("window_merge", "done", "admin")
        finally:
            ws.__exit__(None, None, None)


def test_unforced_merge_is_journaled_without_the_force_marker(tmp_path):
    # Non-vacuity for the marker above: the ordinary click journals the same kind with
    # NO force key, so "was this done through an armed pause" is answerable.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect(client, db_path=str(tmp_path / "curator.db"))
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post("/api/instances/prox/merge_windows", headers=AUTH)
            )
            cmd = _recv(ws)
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"merged": 5}})
            assert fut.result(timeout=5).json() == {"merged": 5}
            detail = json.loads(_actions(db_path)[0][5])
            assert detail == {"manual": True, "merged": 5}
            assert "force" not in detail
        finally:
            ws.__exit__(None, None, None)


def test_refused_merge_writes_no_row(tmp_path):
    # §9 treats busy_dragging as "retry next time, not a failure" — nothing moved, so
    # nothing is journaled (the pass behaves the same way).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect(client, db_path=str(tmp_path / "curator.db"))
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post("/api/instances/prox/merge_windows", headers=AUTH)
            )
            cmd = _recv(ws)
            ws.send_json({"type": "response", "id": cmd["id"], "ok": False,
                          "error": {"code": "busy_dragging", "message": "held"}})
            assert fut.result(timeout=5).status_code == 409
            assert _actions(db_path) == []
        finally:
            ws.__exit__(None, None, None)


async def test_mcp_merge_windows_refuses_under_pause_whatever_the_arguments(tmp_path):
    """§7/§12: an agent is not a human at the keyboard. The MCP tool has no ``force``
    argument, and smuggling one through ``params`` reaches the extension as a junk
    param at most — never the gate, which has already refused. Reddens if the shared
    core ever grows a force path the MCP side can reach."""
    import inspect

    from src.curator import pause as pause_ops
    from src.db.access import Database
    from src.ext.registry import ConnState, Registry
    from src.mcpiface import tools

    # The tool's signature must not even OFFER force.
    assert "force" not in inspect.signature(tools.merge_windows).parameters

    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, msg):
            self.sent.append(msg)

    ws = _WS()
    reg = Registry()
    reg.put("prox", ConnState(ws=ws, conn_epoch=1, install_uuid="u", session_id="s1"))
    app = SimpleNamespace(state=SimpleNamespace(
        db=db, ext_registry=reg,
        settings=SimpleNamespace(cmd_timeout_ms=500, pause_default_min=60),
    ))
    await db.write(lambda c: pause_ops.pause(c, now=int(time.time() * 1000), minutes=60))

    for kwargs in ({}, {"params": {"force": True}}, {"params": {}}):
        try:
            await tools.merge_windows(app, instance="prox", **kwargs)
            raise AssertionError(f"merge_windows must refuse while paused: {kwargs}")
        except tools.ToolError as exc:
            assert exc.code == "paused"
    assert ws.sent == []           # no frame ever reached the socket
    assert await db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM actions").fetchone()
    ) == (0,)
    await db.close()
