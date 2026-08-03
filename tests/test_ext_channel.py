"""WebSocket-level tests of the /ext channel via Starlette's TestClient.

These drive the real endpoint under the ENROLLMENT contract (issue #35):

* a hello authenticates by a per-install SECRET — the client sends the RAW ``secret``
  over TLS, the server hashes it (sha256) and resolves that to an enrolled
  (``status='active'``) instances row and takes the server-assigned id from that row.
  There is no shared token and no trusted self-reported instanceId. A test therefore
  ``approve_instance(...)`` (what a successful enrol creates) before it can drive the
  hello path.
* a not-yet-enrolled client opens with an ``enroll_request`` carrying the window ``code``
  AND the ``instanceId`` it wants; an open window with the right code IS the permission,
  so the server creates the ACTIVE row on the spot and answers
  ``enroll_accepted{instanceId}``. Every refusal creates nothing.

Heartbeat is parked far away by default so no ping steals a synchronous frame; the
two-miss decision is covered as a pure function in test_ext_snapshot.py.
"""

import sqlite3
import time

import pytest
from conftest import _recv, approve_instance, make_settings, secret_for
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.app import create_app
from src.curator.enroll import arm_enroll_window
from src.db import queries


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``."""
    return make_settings(tmp_path, **over)


def _hello(instance_id="i1", **over):
    # Secret-based hello: the RAW secret is hashed server-side and resolves to the approved
    # row for ``instance_id`` (whose stored secret_hash is sha256 of this raw value).
    msg = {
        "type": "hello",
        "protocolVersion": 1,
        "secret": secret_for(instance_id),
        "installUuid": "uuid-A",
        "origin": "chrome-extension://abc",
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
        "secret": "a" * 64,
        # The name the operator typed in the extension. It becomes the instance id
        # verbatim (§6) — there is no separate display title on the wire anymore.
        "instanceId": "my-laptop",
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


def _all_rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn.execute(sql, params).fetchall()
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
            ws.send_json(_hello(secret="deadbeef" * 8))
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "unknown_instance"
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)
        assert client.app.state.ext_rejections == 1


def test_oversized_secret_rejected_auth_no_row(tmp_path):
    # A hostile peer must not make the server hash a multi-MB `secret` on hello; an
    # oversized secret is refused (auth) before hashing, and creates no instances row.
    # Symmetric with the enroll-request cap. Reddens if the hello length cap is removed.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(secret="a" * 5000))
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "auth"
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


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


def test_blank_secret_rejected_auth(tmp_path):
    # A missing/blank secret cannot authenticate => auth, no row created.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(secret="   "))
            ack = _recv(ws)
            assert ack["ok"] is False
            assert ack["error"]["code"] == "auth"
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_non_str_secret_rejected_cleanly(tmp_path):
    # A non-str secret must not crash the isinstance/strip check => clean auth reject.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(secret=12345))
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
            ws.send_json(_hello(secret="c0ffee" * 10, protocolVersion=1))
            ack = _recv(ws)
            assert ack["ok"] is False
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


# --- hello.origin decides nothing -------------------------------------------
def test_any_hello_origin_connects(tmp_path):
    """There is no origin allow-list anymore, so an unexpected origin is not a refusal.

    ``hello.origin`` is now inert on the service side: it is not compared to anything and
    not stored (only the ENROLL request records an origin, for the operator to see at
    approval time). Redden: reinstate the check and these hellos are refused with 'origin'.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        for origin in ("chrome-extension://some-unexpected-id", "https://not-an-ext.example"):
            with client.websocket_connect("/ext") as ws:
                ws.send_json(_hello(origin=origin))
                assert _recv(ws)["ok"] is True, origin


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


# --- enrollment: the window IS the permission (§6) ---------------------------
def test_enroll_request_without_code_at_open_window_creates_nothing(tmp_path):
    # A request WITHOUT a code at an OPEN window is refused (bad_code) and creates NO
    # instance (a missing code reads as code_ok=False).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll())  # no "code"
            frame = _recv(ws)
            assert frame == {"type": "enroll_rejected", "reason": "bad_code"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_enroll_request_valid_code_closed_window_rejected(tmp_path):
    # A request WITH a code but a CLOSED window is refused (closed) and creates nothing.
    # This is the WHOLE gate now — there is no operator approval behind it — so the
    # closed-window branch is load-bearing rather than a first line of defence.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    # window NOT armed => closed
    with TestClient(app) as client:
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code="ABC123"))
            frame = _recv(ws)
            assert frame == {"type": "enroll_rejected", "reason": "closed"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_enroll_request_valid_code_open_window_creates_an_active_instance(tmp_path):
    """The headline: a valid code at an open window enrols IMMEDIATELY.

    One frame in, `enroll_accepted{instanceId}` out, and an ACTIVE row with the client's
    secret_hash — no pending row, no operator step, and the very next hello with the same
    secret authenticates. Reddens if the handler goes back to recording a request instead
    of creating the instance (the hello below would be refused ``unknown_instance``).
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code))
            assert _recv(ws) == {"type": "enroll_accepted", "instanceId": "my-laptop"}
        row = _wait_until(
            lambda: _db_row(
                db_path,
                "SELECT status, secret_hash, install_uuid FROM instances "
                "WHERE id='my-laptop'",
            )
        )
        # The server hashed the raw wire secret on receipt: the stored secret_hash is
        # sha256(raw), never the raw value itself (option A — only the hash is persisted).
        assert row == ("active", queries.sha256_hex("a" * 64), "install-1")
        # The enrolment is audited as the security trail of "a browser joined" — the row
        # an operator's Approve click used to write.
        assert _db_row(
            db_path,
            "SELECT action, initiator, instance_id FROM admin_audit WHERE action='enroll'",
        ) == ("enroll", "system", "my-laptop")

        # And it can hello straight away with that same secret: no second step in between.
        with client.websocket_connect("/ext") as ws2:
            ws2.send_json(_hello(secret="a" * 64))
            ack = _recv(ws2)
            assert ack["ok"] is True and ack["instanceId"] == "my-laptop"
            _recv(ws2)  # snapshot_request


def test_enroll_request_for_a_live_id_is_refused_and_does_not_disturb_it(tmp_path):
    """A name already held by an ACTIVE instance is refused LOUDLY — the one collision the
    removed approval step really guarded against.

    Both halves matter: the newcomer gets ``id_taken`` (so its operator can see why in the
    extension settings — the only place a refusal is visible now), and the incumbent's
    credential is untouched, so it is not silently evicted from the fleet. Reddens if the
    upsert's ``WHERE status != 'active'`` guard is dropped.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "my-laptop")  # already live, with its own secret
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code))
            assert _recv(ws) == {"type": "enroll_rejected", "reason": "id_taken"}
        # The incumbent keeps ITS secret; the newcomer's is nowhere.
        assert _db_row(
            db_path, "SELECT secret_hash FROM instances WHERE id='my-laptop'"
        ) != (queries.sha256_hex("a" * 64),)
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (1,)
        assert _db_row(db_path, "SELECT COUNT(*) FROM admin_audit WHERE action='enroll'") \
            == (0,)


def test_enroll_request_reclaims_a_revoked_id(tmp_path):
    """A REVOKED id may be taken again — the restore path for a revoked MAIN.

    Migration 2 leaves every pre-enrolment row revoked, so without this MAIN could never
    curate again. Reddens if the collision gate is tightened to refuse any EXISTING id.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "my-laptop", status="revoked")
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code))
            assert _recv(ws) == {"type": "enroll_accepted", "instanceId": "my-laptop"}
        assert _wait_until(
            lambda: _db_row(
                db_path,
                "SELECT status, secret_hash FROM instances WHERE id='my-laptop'",
            ),
        ) == ("active", queries.sha256_hex("a" * 64))


def test_enroll_request_with_an_unusable_id_is_refused_under_its_own_reason(tmp_path):
    """A name outside [A-Za-z0-9._-]{1,64} is ``bad_id``, not the generic ``protocol``.

    The id becomes the row's PRIMARY KEY and travels into URLs, metric labels and the
    console. The refusal has its OWN reason because the operator typed the value and has
    to be told WHICH field to fix — the extension prints the reason verbatim. Reddens if
    the check is folded into the structural protocol reject (the settings page would then
    say "protocol mismatch" over a name with a space in it).
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        for bad in ("has space", "x" * 65, "bad/slash", "", None, 42):
            with client.websocket_connect("/ext") as ws:
                ws.send_json(_enroll(code=code, instanceId=bad))
                assert _recv(ws) == {
                    "type": "enroll_rejected", "reason": "bad_id"}, repr(bad)
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_enroll_bad_code_is_decided_before_the_id_is_looked_at(tmp_path):
    """Order: a WRONG CODE wins over an unusable id.

    The code gate must not become an oracle. If ``bad_id`` were decided first, an
    unauthenticated peer could distinguish "my code is wrong" from "my code is right but
    the name is bad" by varying the name — i.e. probe the window code with a frame that is
    deliberately malformed. Reddens if the id check moves ahead of
    ``enroll_reject_reason``.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code="WRONGC", instanceId="has space"))
            assert _recv(ws) == {"type": "enroll_rejected", "reason": "bad_code"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_enroll_request_overlength_uuid_or_secret_rejected_no_row(tmp_path):
    # §36: install_uuid and the raw secret (which becomes the secret_hash credential) land
    # in the DB, so an overlength value is refused as a malformed frame and creates NOTHING
    # (they are NOT truncated — that would corrupt a key/credential).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        for over in ({"installUuid": "u" * 201}, {"secret": "h" * 129}):
            with client.websocket_connect("/ext") as ws:
                ws.send_json(_enroll(code=code, **over))
                assert _recv(ws) == {"type": "enroll_rejected", "reason": "protocol"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_enroll_request_wrong_protocol_rejected(tmp_path):
    # Protocol version gates FIRST, before window/code/id (order in §6).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code, protocolVersion=2))
            assert _recv(ws) == {"type": "enroll_rejected", "reason": "protocol"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


def test_two_enrolments_of_one_name_resolve_to_one_active_row(tmp_path):
    """Two browsers asking for the same name: one is enrolled, the other refused.

    The collision is decided INSIDE the write transaction, which is what makes this
    deterministic rather than last-write-wins. Reddens if the check is moved to a pre-read
    in its own transaction — both would then see "free" and the second would clobber the
    first's credential.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        code = _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code=code, installUuid="install-1", secret="a" * 64))
            assert _recv(ws) == {"type": "enroll_accepted", "instanceId": "my-laptop"}
        _wait_until(
            lambda: _db_row(db_path, "SELECT 1 FROM instances WHERE id='my-laptop'")
        )
        with client.websocket_connect("/ext") as ws2:
            ws2.send_json(_enroll(code=code, installUuid="install-2", secret="b" * 64))
            assert _recv(ws2) == {"type": "enroll_rejected", "reason": "id_taken"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (1,)
        assert _db_row(
            db_path, "SELECT install_uuid FROM instances WHERE id='my-laptop'"
        ) == ("install-1",)


def test_enroll_wrong_protocol_version_touches_no_database(tmp_path):
    """The DB-free gate runs FIRST: a wrong-version enroll_request must not open a single
    reader connection.

    Every ``Database.read`` is a fresh sqlite connection on a thread of the shared pool, and
    this handler runs fully unauthenticated — so a read a hostile peer can trigger before
    its code is checked is a lever on the curator pass, /api/state and /metrics alike.
    Reddens if the window read moves back ahead of the protocol check (the counter below
    goes to 1) or if the removed capacity pre-count returns (2).
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _arm_window(db_path)  # so the second frame below reaches the CODE gate
        db = client.app.state.db
        reads = {"n": 0}
        original = db.read

        async def counting_read(fn):
            reads["n"] += 1
            return await original(fn)

        db.read = counting_read
        try:
            with client.websocket_connect("/ext") as ws:
                ws.send_json(_enroll(code="ABC123", protocolVersion=999))
                assert _recv(ws) == {"type": "enroll_rejected", "reason": "protocol"}
            assert reads["n"] == 0, "a wrong-version frame reached the DB"

            # Non-vacuity: a well-formed frame DOES read — and reads exactly ONCE (the
            # window). The capacity pre-count that used to make it two is gone; capacity is
            # decided inside the write.
            with client.websocket_connect("/ext") as ws2:
                ws2.send_json(_enroll(code="WRONGC"))
                assert _recv(ws2) == {"type": "enroll_rejected", "reason": "bad_code"}
            assert reads["n"] == 1
        finally:
            db.read = original


def test_enroll_non_ascii_code_is_rejected_cleanly(tmp_path):
    """A non-ASCII code must be a plain ``bad_code``, not a 500.

    The code comparison is ``secrets.compare_digest`` (a plain ``==`` leaks the operator's
    window code prefix by timing to an unauthenticated peer), and compare_digest raises
    TypeError on a non-ASCII str — which an anonymous socket can send for free. Reddens if
    the operands stop being encoded before the compare.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _arm_window(db_path)
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_enroll(code="Ж" * 6))
            assert _recv(ws) == {"type": "enroll_rejected", "reason": "bad_code"}
        assert _db_row(db_path, "SELECT COUNT(*) FROM instances") == (0,)


# --- hello field hygiene (§36 symmetry with the enroll path) -----------------
def test_hello_cannot_rename_an_instance(tmp_path):
    """A hello carries no name, and a client that sends one changes nothing.

    ``title`` used to travel on this frame into /admin/instances, /api/state and the
    console, so it had to be clamped and type-checked. The id IS the name now (§6): it is
    assigned once at enrolment and a hello has no field that can move it. Reddens if a
    display name is reintroduced on the authenticated path — an enrolled instance would
    once again be able to redecorate every operator surface, and the megabyte/non-string
    hazards would come back with it.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        # A hello that hopefully carries a name the server does not read.
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello(title="T" * 10_000, instanceId="somethingelse"))
            ack = _recv(ws)
            assert ack["ok"] is True and ack["instanceId"] == "i1"
            _recv(ws)  # snapshot_request
        # The id is unchanged and there is no column for the name to have landed in.
        assert _db_row(db_path, "SELECT id FROM instances") == ("i1",)
        cols = {r[1] for r in _all_rows(db_path, "PRAGMA table_info(instances)")}
        assert "title" not in cols


def test_hello_with_an_unusable_session_id_is_a_protocol_reject(tmp_path):
    """``sessionId`` is IDENTITY, so it is refused rather than truncated.

    The mirror compares this value verbatim against the sessionId later reported in
    snapshots, and a relocation counts as live only while both endpoints' sessions still
    match — clamping it would silently corrupt those comparisons (a truncated session would
    read as a session CHANGE and wipe the instance's tabs). So an oversized or non-string
    session id is a malformed frame, exactly like an oversized install_uuid on the enroll
    path. Reddens if the check is dropped (a 10 MB session id lands in the DB) or softened
    into a clamp.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        for bad in ("s" * 201, 12345, {"a": 1}):
            with client.websocket_connect("/ext") as ws:
                ws.send_json(_hello(sessionId=bad))
                ack = _recv(ws)
                assert ack["ok"] is False, bad
                assert ack["error"]["code"] == "protocol", bad
        # Never registered, never bumped: the row is untouched by the refused hellos.
        assert _db_row(
            db_path, "SELECT connected, conn_epoch, session_id FROM instances WHERE id='i1'"
        ) == (0, 0, None)
        # A missing sessionId stays legal (it is optional) — the reject is about SHAPE.
        with client.websocket_connect("/ext") as ws2:
            msg = _hello()
            del msg["sessionId"]
            ws2.send_json(msg)
            assert _recv(ws2)["ok"] is True


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


def test_silent_socket_quota_compresses_the_first_frame_deadline():
    """Silent sockets are separated from real clients by TIME, not by a second ceiling.

    A reconnect is silent at handshake time too — it must be accepted before it can send
    its hello — so a smaller hard ceiling on silent sockets would refuse reconnects at a
    QUARTER of the connection rate: the same DoS, four times cheaper. What a flood cannot
    fake is speaking promptly, so the quota compresses the first-frame deadline instead:
    over quota, a new socket gets one second rather than ten to identify itself, which
    raises the sustained connection rate needed to hold the pre-auth ceiling by the same
    factor (~13/s -> ~128/s at ENROLL_PREAUTH_MAX=128) and refuses nobody.

    Reddens if the quota is removed (the deadline stops moving) or if it becomes an
    admission check again (this pure test would no longer describe the mechanism).
    """
    from src.ext.channel import (
        _FIRST_FRAME_TIMEOUT_S,
        _FIRST_FRAME_TIMEOUT_UNDER_PRESSURE_S,
        first_frame_timeout_s,
        silent_preauth_max,
    )

    assert silent_preauth_max(128) == 32
    assert silent_preauth_max(8) == 2
    assert silent_preauth_max(1) == 1  # floored: never zero, which would pin the pressure on

    # Idle service: the full window, so an ordinary client on a slow link is unaffected.
    assert first_frame_timeout_s(0, 128) == _FIRST_FRAME_TIMEOUT_S
    assert first_frame_timeout_s(31, 128) == _FIRST_FRAME_TIMEOUT_S
    # Over quota: compressed, and strictly shorter (the whole point).
    assert first_frame_timeout_s(32, 128) == _FIRST_FRAME_TIMEOUT_UNDER_PRESSURE_S
    assert _FIRST_FRAME_TIMEOUT_UNDER_PRESSURE_S < _FIRST_FRAME_TIMEOUT_S


def test_silent_flood_never_refuses_a_reconnect_below_the_ceiling(tmp_path):
    """Sockets over the silent quota are admitted, not refused — the fleet keeps
    reconnecting while the flood is in progress.

    ENROLL_PREAUTH_MAX=8 → silent quota 2. Three silent sockets are held open (over quota,
    so their own deadline is compressed), and an approved instance's hello must still be
    accepted and registered. Reddens if the silent quota is turned back into a refusal:
    the third socket, and then the hello, would be closed at the handshake.
    """
    app = create_app(_settings(tmp_path, enroll_preauth_max=8))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext"), client.websocket_connect("/ext"), \
                client.websocket_connect("/ext"):
            assert client.app.state.ext_silent_count == 3  # over the quota of 2, all live
            with client.websocket_connect("/ext") as ws:
                ws.send_json(_hello())
                assert _recv(ws)["ok"] is True
                _recv(ws)  # snapshot_request
                assert _db_row(
                    db_path, "SELECT connected FROM instances WHERE id='i1'"
                ) == (1,)
        assert client.app.state.ext_silent_count == 0


def test_silent_count_is_released_by_the_first_frame_not_by_the_close(tmp_path):
    """The silent count measures sockets that are CURRENTLY saying nothing.

    A socket that has spoken must not keep counting as silent for the rest of its life —
    otherwise a handful of healthy long-lived connections would put the service into
    permanent "under pressure" mode and hand every new client the 1 s deadline. Reddens if
    the release moves out of the first-frame `finally` and back to the end of the handler.
    """
    app = create_app(_settings(tmp_path, enroll_preauth_max=8))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")
        with client.websocket_connect("/ext") as ws:
            ws.send_json(_hello())
            _recv(ws)  # hello_ack
            _recv(ws)  # snapshot_request
            # A LIVE, connected socket counts as neither silent nor pre-auth.
            assert client.app.state.ext_silent_count == 0
            assert client.app.state.ext_preauth_count == 0


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
            ws2.send_json(_hello(secret="beef" * 16))
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
