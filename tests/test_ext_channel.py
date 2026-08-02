"""WebSocket-level tests of the /ext channel via Starlette's TestClient.

These drive the real endpoint (accept -> hello -> hello_ack -> snapshot_request,
reject paths, epoch eviction, duplicate rejection). Heartbeat is set very large so
no ping interferes with the short synchronous exchanges; the two-miss decision is
covered as a pure function in test_ext_snapshot.py.
"""

import sqlite3
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.app import create_app
from src.db import queries

EXT_TOKEN = "test-ext-token"


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0",
        port=8000,
        heartbeat_ms=600_000,  # far larger than any test => no ping interferes
        protocol_version=1,
        ext_token=EXT_TOKEN,
        ext_allowed_origins="",
    )
    s.update(over)
    return SimpleNamespace(**s)


def _hello(**over):
    msg = {
        "type": "hello",
        "protocolVersion": 1,
        "token": EXT_TOKEN,
        "instanceId": "i1",
        "installUuid": "uuid-A",
        "origin": "chrome-extension://abc",
        "title": "Themed",
        "sessionId": "sess-1",
        "allowExecuteJs": False,
    }
    msg.update(over)
    return msg


def _db_row(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _wait_until(fn, timeout=5.0, interval=0.01):
    """Poll ``fn`` until it returns truthy or the timeout elapses (committed DB
    writes land asynchronously on the server thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = fn()
        if val:
            return val
        time.sleep(interval)
    return fn()


# --- hello happy path -------------------------------------------------------
def test_hello_happy_path_acks_and_requests_snapshot(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            ack = ws.receive_json()
            assert ack["type"] == "hello_ack"
            assert ack["ok"] is True
            assert ack["instanceId"] == "i1"
            assert isinstance(ack["connEpoch"], int)

            req = ws.receive_json()
            assert req["type"] == "snapshot_request"
            assert "id" in req

            row = _db_row(
                db_path,
                "SELECT connected, session_id, conn_epoch FROM instances WHERE id='i1'",
            )
            assert row == (1, "sess-1", ack["connEpoch"])


# --- protocol / auth / instance rejects -------------------------------------
def test_wrong_protocol_version_rejected_and_recorded(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(protocolVersion=999))
            ack = ws.receive_json()
            assert ack == {"type": "hello_ack", "ok": False, "error": {"code": "protocol"}}
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert _db_row(db_path, "SELECT reject_reason FROM instances WHERE id='i1'") == (
            "protocol",
        )
        assert client.app.state.ext_rejections == 1


def test_wrong_token_rejected(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(token="nope"))
            ack = ws.receive_json()
            assert ack["ok"] is False
            assert ack["error"]["code"] == "auth"
        assert _db_row(db_path, "SELECT reject_reason FROM instances WHERE id='i1'") == (
            "auth",
        )


def test_missing_instance_id_rejected_without_db_row(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(instanceId="   "))
            ack = ws.receive_json()
            assert ack["ok"] is False
            assert ack["error"]["code"] == "instance"
        # A blank instanceId cannot key an instances row; only the counter moved.
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)
        assert client.app.state.ext_rejections == 1


# --- origin allow-list ------------------------------------------------------
def test_origin_not_in_allowlist_rejected(tmp_path):
    app = create_app(_settings(tmp_path, ext_allowed_origins="chrome-extension://good"))
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(origin="chrome-extension://evil"))
            ack = ws.receive_json()
            assert ack["ok"] is False
            assert ack["error"]["code"] == "origin"


# --- headline: old socket doesn't clobber the new one -----------------------
def test_old_socket_disconnect_does_not_clobber_new(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws1:
            ws1.send_json(_hello())  # installUuid uuid-A
            e1 = ws1.receive_json()["connEpoch"]
            ws1.receive_json()  # snapshot_request

            # Reconnect with the SAME installUuid => legitimate takeover: evicts
            # ws1 and bumps the epoch.
            with client.websocket_connect("/ext") as ws2:
                ws2.send_json(_hello())
                e2 = ws2.receive_json()["connEpoch"]
                ws2.receive_json()  # snapshot_request
                assert e2 == e1 + 1

                # DB now reflects the live newer connection.
                assert _db_row(
                    db_path, "SELECT connected, conn_epoch FROM instances WHERE id='i1'"
                ) == (1, e2)

                # Simulate the OLD socket's finalizer running LATE with its own
                # stale epoch e1 (the real queries.mark_disconnected the channel
                # uses). The epoch guard must make it a no-op.
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("PRAGMA busy_timeout = 5000")
                    queries.mark_disconnected(conn, "i1", e1)
                    conn.commit()
                finally:
                    conn.close()

                connected = _db_row(
                    db_path, "SELECT connected FROM instances WHERE id='i1'"
                )[0]
                assert connected == 1  # drop the epoch guard => this becomes 0


# --- duplicate instance rejected, live socket survives ----------------------
def test_duplicate_instance_rejected_first_socket_survives(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws1:
            ws1.send_json(_hello(installUuid="uuid-A"))
            e1 = ws1.receive_json()["connEpoch"]
            req = ws1.receive_json()  # snapshot_request for ws1

            # Second hello, SAME instanceId but a DIFFERENT installUuid => copied
            # bundle => duplicate. It must be rejected and closed.
            with client.websocket_connect("/ext") as ws2:
                ws2.send_json(_hello(installUuid="uuid-B"))
                ack2 = ws2.receive_json()
                assert ack2["ok"] is False
                assert ack2["error"]["code"] == "duplicate_instance"
                with pytest.raises(WebSocketDisconnect):
                    ws2.receive_json()

            assert client.app.state.ext_rejections == 1
            # The first connection is untouched: same epoch, still connected.
            assert _db_row(
                db_path, "SELECT connected, conn_epoch FROM instances WHERE id='i1'"
            ) == (1, e1)

            # And it still WORKS: a snapshot sent on ws1 applies.
            ws1.send_json(
                {
                    "type": "snapshot",
                    "id": req["id"],
                    "sessionId": "sess-1",
                    "focusedWindowId": 1,
                    "tabs": [
                        {
                            "tabId": 7,
                            "windowId": 1,
                            "url": "https://x",
                            "title": "x",
                            "favIconUrl": None,
                            "pinned": False,
                            "active": True,
                            "audible": False,
                            "ageMs": 0,
                            "openedAgoMs": 0,
                            "ageUnknown": False,
                            "selfNavigating": False,
                        }
                    ],
                    "windows": [{"id": 1, "type": "normal", "state": "normal"}],
                }
            )
            got = _wait_until(
                lambda: _db_row(
                    db_path, "SELECT tab_id FROM tabs WHERE instance_id='i1' AND tab_id=7"
                )
            )
            assert got == (7,), "the surviving first socket still applies snapshots"


# --- heartbeat wiring (integration, not just the pure step) ------------------
def test_heartbeat_closes_after_two_missed_pongs(tmp_path):
    # With a small interval and NO pong ever sent, the server must ping then close
    # the socket after two consecutive misses (§6). Exercises the _heartbeat
    # coroutine wiring, not just the pure heartbeat_step.
    app = create_app(_settings(tmp_path, heartbeat_ms=80))
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            assert ws.receive_json()["type"] == "hello_ack"
            assert ws.receive_json()["type"] == "snapshot_request"
            # Receive ping frames without answering; the socket closes after the
            # second miss => WebSocketDisconnect. range() gives ample headroom.
            with pytest.raises(WebSocketDisconnect):
                for _ in range(10):
                    frame = ws.receive_json()
                    assert frame["type"] == "ping"


def test_heartbeat_pong_keeps_connection_alive(tmp_path):
    # Answering each ping with a pong resets the miss counter, so the connection
    # stays open across several intervals and the instance stays connected.
    app = create_app(_settings(tmp_path, heartbeat_ms=80))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            ws.receive_json()  # hello_ack
            ws.receive_json()  # snapshot_request
            for _ in range(4):
                frame = ws.receive_json()
                assert frame["type"] == "ping"  # never a close while we pong
                ws.send_json({"type": "pong"})
            assert _db_row(db_path, "SELECT connected FROM instances WHERE id='i1'") == (1,)
