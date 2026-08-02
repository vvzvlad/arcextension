"""WebSocket-level tests of the /ext channel via Starlette's TestClient.

These drive the real endpoint under the ENROLLMENT contract (issue #35):

* a hello authenticates by a per-install SECRET — the client sends ``secretHash``, the
  server resolves it to an APPROVED (``status='active'``) instances row and takes the
  server-assigned id from that row. There is no shared token and no trusted
  self-reported instanceId. A test therefore ``approve_instance(...)`` (what Task E's
  operator approval does) before it can drive the hello path.
* a not-yet-approved client opens with an ``enroll_request`` carrying the window
  ``code``; the server records the request and answers ``enroll_pending`` — it never
  creates an instances row.

Heartbeat is parked far away by default so no ping steals a synchronous frame; the
two-miss decision is covered as a pure function in test_ext_snapshot.py.
"""

import sqlite3
import time

import pytest
from conftest import _recv, approve_instance, make_settings, secret_hash_for
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.app import create_app
from src.curator.enroll import arm_enroll_window
from src.db import queries


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``."""
    return make_settings(tmp_path, **over)


def _hello(instance_id="i1", **over):
    # Secret-based hello: the secretHash resolves to the approved row for ``instance_id``.
    msg = {
        "type": "hello",
        "protocolVersion": 1,
        "secretHash": secret_hash_for(instance_id),
        "installUuid": "uuid-A",
        "origin": "chrome-extension://abc",
        "title": "Themed",
        "sessionId": "sess-1",
        "allowExecuteJs": False,
    }
    msg.update(over)
    return msg


def _enroll(**over):
    msg = {
        "type": "enroll_request",
        "protocolVersion": 1,
        "installUuid": "install-1",
        "secretHash": "a" * 64,
        "origin": "chrome-extension://abc",
        "title": "My laptop",
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


def _arm_window(db_path, minutes=10, now=None):
    """Open the enrollment window directly in the DB and return its code."""
    now = int(time.time() * 1000) if now is None else now
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        state = arm_enroll_window(conn, now=now, minutes=minutes)
        conn.commit()
        return state.code
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


# --- hello happy path (secret-based) ----------------------------------------
def test_hello_happy_path_acks_and_requests_snapshot(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            ack = _recv(ws)
            assert ack["type"] == "hello_ack"
            assert ack["ok"] is True
            # The id is SERVER-assigned (from the resolved row), not self-reported.
            assert ack["instanceId"] == "i1"
            assert isinstance(ack["connEpoch"], int)

            req = _recv(ws)
            assert req["type"] == "snapshot_request"
            assert "id" in req

            row = _db_row(
                db_path,
                "SELECT connected, session_id, conn_epoch FROM instances WHERE id='i1'",
            )
            assert row == (1, "sess-1", ack["connEpoch"])
            # The pre-auth slot is released on a successful hello, BEFORE the receive
            # loop — a live connection never occupies the pre-auth ceiling.
            assert client.app.state.ext_preauth_count == 0


# --- protocol / auth / secret-status rejects --------------------------------
def test_wrong_protocol_version_rejected_and_recorded(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(protocolVersion=999))
            ack = _recv(ws)
            assert ack == {"type": "hello_ack", "ok": False, "error": {"code": "protocol"}}
            with pytest.raises(WebSocketDisconnect):
                _recv(ws)
        # The id resolved to a real active row, so the reject reason is recorded on it.
        assert _db_row(db_path, "SELECT reject_reason FROM instances WHERE id='i1'") == (
            "protocol",
        )
        assert client.app.state.ext_rejections == 1


def test_unknown_secret_rejected_unknown_instance(tmp_path):
    # A secret that matches no instances row => unknown_instance, and — the load-bearing
    # invariant — NO instances row is created (record_rejection is UPDATE-only now, §3).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(secretHash="deadbeef" * 8))
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "unknown_instance"
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)
        assert client.app.state.ext_rejections == 1


def test_revoked_secret_rejected_revoked(tmp_path):
    # A known-but-revoked instance => the client-facing 'revoked' verdict (§7), and the
    # reject is recorded on that revoked row (which stays revoked).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1", status="revoked")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "revoked"
        assert _db_row(
            db_path, "SELECT status, reject_reason, connected FROM instances WHERE id='i1'"
        ) == ("revoked", "revoked", 0)


def test_pending_secret_rejected_unknown_instance(tmp_path):
    # An approved-not-yet ('pending') row => unknown_instance so the client keeps waiting
    # rather than acting on a 'revoked' verdict.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1", status="pending")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "unknown_instance"
        # A pending row was never bumped: still disconnected, epoch untouched.
        assert _db_row(
            db_path, "SELECT connected, conn_epoch FROM instances WHERE id='i1'"
        ) == (0, 0)


def test_blank_secret_hash_rejected_auth(tmp_path):
    # A missing/blank secretHash cannot authenticate => auth, no row created.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(secretHash="   "))
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "auth"
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_non_str_secret_hash_rejected_cleanly(tmp_path):
    # A non-str secretHash must not crash the isinstance/strip check => clean auth reject.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(secretHash=12345))
            ack = _recv(ws)
            assert ack["ok"] is False and ack["error"]["code"] == "auth"


# --- anon hello never creates an instances row (acc 10, replaces vacuous test) ---
def test_anon_hello_with_public_protocol_creates_no_instances_row(tmp_path):
    # Acceptance 10 / §3: an anon who knows only the public PROTOCOL_VERSION but has no
    # valid secret must not be able to create an instances row. Here the unknown secret
    # resolves to None, so the channel rejects with instance_id=None and NEVER reaches
    # _RECORD_REJECTION / _HELLO_UPSERT — the resolve→None gate is what guards this path.
    # (That the UPSERTs are themselves UPDATE-only — the OTHER half of §3 — is pinned
    # separately at the query level: test_record_rejection_is_update_only_no_row_created
    # and test_hello_upsert_is_update_only_and_returns_none_for_missing_row.)
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(secretHash="c0ffee" * 10, protocolVersion=1))
            ack = _recv(ws)
            assert ack["ok"] is False
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


# --- origin allow-list ------------------------------------------------------
def test_origin_not_in_allowlist_rejected(tmp_path):
    app = create_app(_settings(tmp_path, ext_allowed_origins="chrome-extension://good"))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(origin="chrome-extension://evil"))
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "origin"


# --- headline: old socket doesn't clobber the new one -----------------------
def test_old_socket_disconnect_does_not_clobber_new(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws1:
            ws1.send_json(_hello())  # installUuid uuid-A
            e1 = _recv(ws1)["connEpoch"]
            _recv(ws1)  # snapshot_request

            # Reconnect with the SAME secret + installUuid => legitimate takeover: evicts
            # ws1 and bumps the epoch (the UPDATE-only upsert still does conn_epoch+1).
            with client.websocket_connect("/ext") as ws2:
                ws2.send_json(_hello())
                e2 = _recv(ws2)["connEpoch"]
                _recv(ws2)  # snapshot_request
                assert e2 == e1 + 1

                # DB now reflects the live newer connection.
                assert _db_row(
                    db_path, "SELECT connected, conn_epoch FROM instances WHERE id='i1'"
                ) == (1, e2)

                # Simulate the OLD socket's finalizer running LATE with its own stale
                # epoch e1. The epoch guard must make it a no-op.
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
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws1:
            ws1.send_json(_hello(installUuid="uuid-A"))
            e1 = _recv(ws1)["connEpoch"]
            req = _recv(ws1)  # snapshot_request for ws1

            # Second hello, SAME secret (=> same resolved id) but a DIFFERENT installUuid
            # => copied bundle => duplicate. It must be rejected and closed.
            with client.websocket_connect("/ext") as ws2:
                ws2.send_json(_hello(installUuid="uuid-B"))
                ack2 = _recv(ws2)
                assert ack2["ok"] is False
                assert ack2["error"]["code"] == "duplicate_instance"
                with pytest.raises(WebSocketDisconnect):
                    _recv(ws2)

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


# --- degraded /ext is refused (§12) -----------------------------------------
def test_degraded_ext_rejected_no_bump_no_registry(tmp_path):
    # Non-vacuous: an APPROVED active row exists, yet a degraded /ext must short-circuit
    # BEFORE the hello would bump it. So connected stays 0, no epoch bump, nothing
    # registered — proving the degraded close happens before any hello handling.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        client.app.state.degraded = True
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            with pytest.raises(WebSocketDisconnect):
                _recv(ws)  # accept then close 1011, no hello handling
        assert _db_row(
            db_path, "SELECT connected, conn_epoch FROM instances WHERE id='i1'"
        ) == (0, 0)
        assert client.app.state.ext_registry.get("i1") is None


# --- enrollment: gates before any row is written ----------------------------
def test_enroll_request_without_code_at_open_window_writes_no_row(tmp_path):
    # Acceptance 2: a request WITHOUT a code at an OPEN window is refused (bad_code) and
    # writes NO enroll_requests row (a missing code reads as code_ok=False).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll())  # no "code"
            frame = _recv(ws)
            assert frame == {"type": "enroll_rejected", "reason": "bad_code"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM enroll_requests") == (0,)


def test_enroll_request_valid_code_closed_window_rejected(tmp_path):
    # Acceptance 3: a request WITH a code but a CLOSED window is refused (closed), 0 rows.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    # window NOT armed => closed
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code="ABC123"))
            frame = _recv(ws)
            assert frame == {"type": "enroll_rejected", "reason": "closed"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM enroll_requests") == (0,)


def test_enroll_request_valid_code_open_window_pending_and_no_first_seen_bump(tmp_path):
    # Acceptance 2 (accept half): a valid code at an open window => enroll_pending + one
    # row; a REPEAT enroll_request refreshes last_seen_at but NOT first_seen_at (so the
    # request can still age out against its original first_seen_at, issue §1).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code, title="First"))
            assert _recv(ws) == {"type": "enroll_pending"}
        row1 = _wait_until(
            lambda: _db_row(
                db_path,
                "SELECT first_seen_at, last_seen_at, suggested_title, secret_hash, "
                "protocol_version FROM enroll_requests WHERE install_uuid='install-1'",
            )
        )
        assert row1 is not None
        first_seen, last_seen1, title1, sh1, pv = row1
        assert title1 == "First"
        assert sh1 == "a" * 64
        assert pv == 1
        assert _db_row(db_path, "SELECT COUNT(*) FROM enroll_requests") == (1,)

        time.sleep(0.02)  # ensure a later wall-clock for last_seen_at
        with client.websocket_connect("/ext") as ws2:
            ws2.send_json(_enroll(code=code, title="Second"))
            assert _recv(ws2) == {"type": "enroll_pending"}
        row2 = _wait_until(
            lambda: (
                lambda r: r if r and r[2] == "Second" else None
            )(
                _db_row(
                    db_path,
                    "SELECT first_seen_at, last_seen_at, suggested_title "
                    "FROM enroll_requests WHERE install_uuid='install-1'",
                )
            )
        )
        first_seen2, last_seen2, title2 = row2
        # Still one row; first_seen_at is FROZEN; last_seen_at and title were refreshed.
        assert _db_row(db_path, "SELECT COUNT(*) FROM enroll_requests") == (1,)
        assert first_seen2 == first_seen
        assert last_seen2 >= last_seen1
        assert title2 == "Second"


def test_enroll_request_at_capacity_rejected_no_row(tmp_path):
    # The capacity gate refuses an enroll_request once the pending list is at the ceiling
    # and writes no NEW row. enroll_max_pending=0 makes the ceiling bite immediately.
    app = create_app(_settings(tmp_path, enroll_max_pending=0))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code))
            assert _recv(ws) == {"type": "enroll_rejected", "reason": "capacity"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM enroll_requests") == (0,)


def test_enroll_request_overlength_uuid_or_hash_rejected_no_row(tmp_path):
    # §36: install_uuid (PK) and secret_hash (credential) go verbatim into the operator-
    # facing pending list, so an overlength value is refused as a malformed frame and
    # writes NO row (they are NOT truncated — that would corrupt a key/credential).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        for over in ({"installUuid": "u" * 201}, {"secretHash": "h" * 129}):
            with client.websocket_connect("/ext") as ws:
                ws.send_json(_enroll(code=code, **over))
                assert _recv(ws) == {"type": "enroll_rejected", "reason": "protocol"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM enroll_requests") == (0,)


def test_enroll_request_wrong_protocol_rejected(tmp_path):
    # Protocol version gates FIRST, before window/code/capacity (order in §2).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code, protocolVersion=2))
            assert _recv(ws) == {"type": "enroll_rejected", "reason": "protocol"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM enroll_requests") == (0,)


# --- first-frame timeout closes the socket (§2) -----------------------------
def test_first_frame_timeout_closes(tmp_path, monkeypatch):
    # An accepted socket that never sends a first frame is closed after the timeout.
    # The 10s default is monkeypatched down so the test is fast.
    import src.ext.channel as channel_mod

    monkeypatch.setattr(channel_mod, "_FIRST_FRAME_TIMEOUT_S", 0.2)
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            with pytest.raises(WebSocketDisconnect):
                _recv(ws, timeout=3.0)  # server closes after ~0.2s of silence


# --- pre-auth ceiling refuses the Nth socket, /healthz still answers (acc 15) ---
def test_preauth_ceiling_refuses_and_healthz_still_answers(tmp_path):
    app = create_app(_settings(tmp_path, enroll_preauth_max=1))
    with TestClient(app) as client:
        # ws1 is accepted but never says hello => it holds the single pre-auth slot.
        with client.websocket_connect("/ext") as ws1:  # noqa: F841
            assert client.app.state.ext_preauth_count == 1
            # ws2 is refused at the handshake, BEFORE accept (a pre-accept close).
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ext"):
                    pass
            # The health endpoint keeps answering while the ceiling bites.
            assert client.get("/healthz").status_code == 200
        # The refusal was counted.
        assert client.app.state.ext_rejections == 1


def test_preauth_slot_released_after_hello_and_after_reject(tmp_path):
    # A successful hello releases its slot before the receive loop; a rejected hello
    # releases it too. Either leak would eventually wedge the pre-auth ceiling shut.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            _recv(ws)  # hello_ack
            _recv(ws)  # snapshot_request
            assert client.app.state.ext_preauth_count == 0
        # A rejected hello also leaves the counter at 0.
        with client.websocket_connect("/ext") as ws2:
            ws2.send_json(_hello(secretHash="beef" * 16))
            _recv(ws2)
        assert client.app.state.ext_preauth_count == 0


# --- a snapshot with NO id must be ignored, never wipe tabs ------------------
def test_snapshot_without_id_is_ignored(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    tab = {"tabId": 1, "windowId": 1, "url": "https://a", "title": "a",
           "favIconUrl": None, "pinned": False, "active": True, "audible": False,
           "ageMs": 0, "openedAgoMs": 0, "ageUnknown": False, "selfNavigating": False}
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            _recv(ws)                     # hello_ack
            req = _recv(ws)               # snapshot_request
            ws.send_json({"type": "snapshot", "id": req["id"], "sessionId": "sess-1",
                          "focusedWindowId": 1, "tabs": [tab],
                          "windows": [{"id": 1, "type": "normal", "state": "normal"}]})
            _wait_until(lambda: _db_row(
                db_path, "SELECT COUNT(*) FROM tabs WHERE instance_id='i1'") == (1,))
            # Unsolicited snapshot with NO id and NO sessionId: the old None==None
            # bypass would treat it as a session change and WIPE the tab. It must be
            # ignored — the tab survives.
            ws.send_json({"type": "snapshot", "focusedWindowId": 9, "tabs": [], "windows": []})
            time.sleep(0.15)
            assert _db_row(db_path, "SELECT COUNT(*) FROM tabs WHERE instance_id='i1'") == (1,)


# --- heartbeat wiring (integration, not just the pure step) ------------------
def test_heartbeat_closes_after_two_missed_pongs(tmp_path):
    app = create_app(_settings(tmp_path, heartbeat_ms=80))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            assert _recv(ws)["type"] == "hello_ack"
            assert _recv(ws)["type"] == "snapshot_request"
            with pytest.raises(WebSocketDisconnect):
                for _ in range(10):
                    frame = _recv(ws)
                    assert frame["type"] == "ping"


def test_heartbeat_pong_keeps_connection_alive(tmp_path):
    app = create_app(_settings(tmp_path, heartbeat_ms=80))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            _recv(ws)  # hello_ack
            _recv(ws)  # snapshot_request
            for _ in range(4):
                frame = _recv(ws)
                assert frame["type"] == "ping"  # never a close while we pong
                ws.send_json({"type": "pong"})
            assert _db_row(db_path, "SELECT connected FROM instances WHERE id='i1'") == (1,)
