"""GET /api/state + POST /api/focus (§10), plus the single-flight refresh guard.

The websocket acts as the extension exactly as in test_restore: it answers the
initial snapshot_request so the instance is fresh, and answers /api/focus's
``focus_tab`` command. The HTTP call runs on a background thread so the same test
thread can drive the websocket while the request is in flight.
"""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from starlette.testclient import TestClient

from src.api.state import kick_state_refresh
from src.app import create_app

EXT_TOKEN = "test-ext-token"
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0",
        port=8000,
        heartbeat_ms=600_000,
        protocol_version=1,
        ext_token=EXT_TOKEN,
        ext_allowed_origins="",
        cmd_timeout_ms=2000,
        snapshot_timeout_ms=2000,
        state_fresh_ms=3_000_000,
        restore_exemption_min=120,
        actions_retention_days=90,
        js_audit_retention_days=730,
        pass_interval_min=5,
        idle_minutes=60,
        main_instance_id="main",
    )
    s.update(over)
    return SimpleNamespace(**s)


def _hello(instance_id="i1", session="sess-1", **over):
    msg = {
        "type": "hello",
        "protocolVersion": 1,
        "token": EXT_TOKEN,
        "instanceId": instance_id,
        "installUuid": "uuid-A",
        "origin": "chrome-extension://abc",
        "title": "Themed",
        "sessionId": session,
        "allowExecuteJs": False,
    }
    msg.update(over)
    return msg


def _snapshot(req_id, tabs, session="sess-1"):
    return {
        "type": "snapshot",
        "id": req_id,
        "sessionId": session,
        "focusedWindowId": 1,
        "tabs": tabs,
        "windows": [{"id": 1, "type": "normal", "state": "normal"}],
    }


def _db_row(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _wait_until(fn, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = fn()
        if val:
            return val
        time.sleep(interval)
    return fn()


def _connect_fresh(client, db_path, instance_id="i1", session="sess-1", tabs=None):
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    ws.receive_json()                 # hello_ack
    req = ws.receive_json()           # snapshot_request
    ws.send_json(_snapshot(req["id"], tabs or [], session=session))
    _wait_until(
        lambda: _db_row(
            db_path, "SELECT snapshot_at FROM instances WHERE id=?", (instance_id,)
        )[0]
        is not None
    )
    return ws


def _seed_quick_link(db_path, url, title, position):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO quick_links (url, title, position, created_at) "
            "VALUES (?,?,?,?)",
            (url, title, position, 1),
        )
        conn.commit()
    finally:
        conn.close()


# --- auth + degraded --------------------------------------------------------
def test_state_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 401
        assert client.get(
            "/api/state", headers={"Authorization": "Bearer nope"}
        ).status_code == 401
        client.app.state.degraded = True
        assert client.get("/api/state", headers=AUTH).status_code == 503


# --- shape: mirror returned immediately -------------------------------------
def test_state_returns_mirror_shape(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(
            client, db_path,
            tabs=[{"tabId": 7, "windowId": 1, "url": "https://a/b", "title": "A"}],
        )
        try:
            _seed_quick_link(db_path, "https://ql/1", "QL1", 0)
            resp = client.get("/api/state", headers=AUTH)
            assert resp.status_code == 200
            body = resp.json()
            # Exact §10 StateResponse top-level shape.
            assert set(body.keys()) == {
                "server_now", "last_pass_at", "last_pass_ok", "rules_total",
                "rules_invalid", "instances", "tabs", "quick_links",
            }
            assert isinstance(body["server_now"], int)
            assert body["rules_total"] == 0 and body["rules_invalid"] == 0
            # The instance is present with the §10 fields.
            inst = {i["id"]: i for i in body["instances"]}["i1"]
            assert inst["connected"] is True
            assert set(inst.keys()) == {
                "id", "title", "connected", "snapshot_at", "last_seen_at",
                "reject_reason", "reject_at", "focused_window_id",
            }
            # The tab from the snapshot is mirrored with the §10 tab fields.
            tab = {t["tab_id"]: t for t in body["tabs"]}[7]
            assert tab["instance_id"] == "i1" and tab["url"] == "https://a/b"
            assert set(tab.keys()) == {
                "instance_id", "tab_id", "window_id", "url", "title",
                "fav_icon_url", "pinned", "active", "audible", "last_active_at",
                "age_unknown",
            }
            # Quick links present, ordered by position.
            assert [q["url"] for q in body["quick_links"]] == ["https://ql/1"]
        finally:
            ws.__exit__(None, None, None)


# --- immediate return + background refresh kicked on a stale mirror ----------
def test_state_kicks_background_refresh_when_stale(tmp_path):
    # state_fresh_ms=1 => the mirror is stale immediately after the initial
    # snapshot, so GET /api/state must kick a background refresh: the ws receives a
    # NEW snapshot_request. snapshot_timeout small so the detached poll-task dies
    # quickly. If the refresh were removed, no second snapshot_request would arrive.
    app = create_app(_settings(tmp_path, state_fresh_ms=1, snapshot_timeout_ms=300))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            resp = client.get("/api/state", headers=AUTH)
            assert resp.status_code == 200
            # The background task (after the response) kicked a refresh: a fresh
            # snapshot_request is now waiting on the socket.
            frame = ws.receive_json()
            assert frame["type"] == "snapshot_request"
        finally:
            ws.__exit__(None, None, None)


# --- POST /api/focus success (foreign jump) ---------------------------------
def test_focus_sends_focus_tab_and_returns_ok(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/focus", json={"instance": "i1", "tabId": 42}, headers=AUTH
                )
            )
            cmd = ws.receive_json()
            assert cmd["type"] == "command" and cmd["command"] == "focus_tab"
            assert cmd["params"] == {"tabId": 42}
            assert cmd["sessionId"] == "sess-1"     # current session stamped (§5)
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True, "result": {}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200 and resp.json() == {"ok": True}
        finally:
            ws.__exit__(None, None, None)


# --- POST /api/focus no_such_tab => clear 409 so the page re-fetches ---------
def test_focus_no_such_tab_is_clear_error(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/focus", json={"instance": "i1", "tabId": 999}, headers=AUTH
                )
            )
            cmd = ws.receive_json()
            ws.send_json({
                "type": "response", "id": cmd["id"], "ok": False,
                "error": {"code": "no_such_tab", "message": "gone"},
            })
            resp = fut.result(timeout=5)
            assert resp.status_code == 409
            body = resp.json()
            # Clear, actionable error: the page re-fetches /api/state (never silent).
            assert body["ok"] is False
            assert body["error"] == "no_such_tab"
            assert body["refetch"] is True
        finally:
            ws.__exit__(None, None, None)


# --- POST /api/focus with no live socket => 502, not a hang -----------------
def test_focus_no_connection_is_502(tmp_path):
    app = create_app(_settings(tmp_path, cmd_timeout_ms=300))
    with TestClient(app) as client:
        resp = client.post(
            "/api/focus", json={"instance": "ghost", "tabId": 1}, headers=AUTH
        )
        assert resp.status_code == 502
        assert resp.json()["error"] == "no_connection"


# --- single-flight unit test: two kicks -> ONE snapshot_request -------------
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Answers exactly the two SELECTs the refresh path issues: the connected-ages
    scan and the per-instance freshness read. Both report a STALE mirror."""

    def __init__(self, instance_id, session_id, snapshot_at):
        self.row_factory = None
        self._ages = [{"id": instance_id, "snapshot_at": snapshot_at}]
        self._fresh = (1, session_id, snapshot_at)  # (connected, session, snapshot_at)

    def execute(self, sql, params=()):
        if "connected = 1" in sql:
            return _FakeCursor(self._ages)
        if "connected, session_id, snapshot_at" in sql:
            return _FakeCursor([self._fresh])
        return _FakeCursor([])


class _FakeDb:
    def __init__(self, conn):
        self._conn = conn

    async def read(self, fn):
        return fn(self._conn)


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


class _FakeConnState:
    def __init__(self, session_id):
        self.session_id = session_id
        self.ws = _FakeWs()
        self.state_refresh_inflight = False
        self.pending_snapshot_id = None
        self.pending_sent_at = None


class _FakeRegistry:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, instance_id):
        return self._m.get(instance_id)


def test_state_refresh_is_single_flight():
    async def scenario():
        cs = _FakeConnState("sess-1")
        registry = _FakeRegistry({"i1": cs})
        # snapshot_at far in the past => stale for any reasonable STATE_FRESH_MS.
        db = _FakeDb(_FakeConn("i1", "sess-1", snapshot_at=0))
        app = SimpleNamespace(
            state=SimpleNamespace(ext_registry=registry, state_refresh_tasks=set())
        )
        settings = SimpleNamespace(state_fresh_ms=1000, snapshot_timeout_ms=100)

        await kick_state_refresh(app, db, settings)  # sends #1, sets the flag
        await kick_state_refresh(app, db, settings)  # flag set => must NOT send
        # Drain the detached clear-tasks so no task is left pending.
        await asyncio.gather(*list(app.state.state_refresh_tasks))
        return cs.ws.sent

    sent = asyncio.run(scenario())
    requests = [f for f in sent if f.get("type") == "snapshot_request"]
    # EXACTLY one: the single-flight guard collapsed the second kick. Remove the
    # `state_refresh_inflight` guard and this becomes two (the mutation reddens).
    assert len(requests) == 1
