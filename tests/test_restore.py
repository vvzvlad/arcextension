"""POST /api/actions/:id/restore (§10) via TestClient (websocket + HTTP).

The websocket acts as the extension: it answers the initial snapshot_request so
the source instance is FRESH, then answers the service's ``open_tab`` command. The
HTTP POST runs in a background thread so the same test thread can drive the
websocket while the request is in flight.
"""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from starlette.testclient import TestClient

from src.app import create_app
from src.db.actions import insert_action

EXT_TOKEN = "test-ext-token"
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0",
        port=8000,
        heartbeat_ms=600_000,       # no ping interferes
        protocol_version=1,
        ext_token=EXT_TOKEN,
        ext_allowed_origins="",
        cmd_timeout_ms=2000,
        snapshot_timeout_ms=2000,
        state_fresh_ms=3_000_000,   # keep the seeded snapshot "fresh" for the test
        restore_exemption_min=120,
        actions_retention_days=90,
        js_audit_retention_days=730,
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


def _seed_action(db_path, **kw):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        kw.setdefault("ts", 1_000_000)
        aid = insert_action(conn, **kw)
        conn.commit()
        return aid
    finally:
        conn.close()


def _seed_tab(db_path, instance_id, tab_id, url):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, "
            "fav_icon_url, pinned, active, opened_at, last_active_at, age_unknown, "
            "self_navigating, audible, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instance_id, tab_id, 1, url, "t", None, 0, 0, 0, 0, 0, 0, 0, 0),
        )
        conn.commit()
    finally:
        conn.close()


def _connect_fresh(client, db_path, instance_id="i1", session="sess-1", tabs=None):
    """hello + answer the initial snapshot_request so the instance is FRESH."""
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    ws.receive_json()                # hello_ack
    req = ws.receive_json()          # snapshot_request
    ws.send_json(_snapshot(req["id"], tabs or [], session=session))
    _wait_until(
        lambda: _db_row(
            db_path, "SELECT snapshot_at FROM instances WHERE id=?", (instance_id,)
        )[0]
        is not None
    )
    return ws


# --- auth + degraded --------------------------------------------------------
def test_restore_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # No token / wrong token => 401 (before any work).
        assert client.post("/api/actions/1/restore").status_code == 401
        assert client.post(
            "/api/actions/1/restore", headers={"Authorization": "Bearer nope"}
        ).status_code == 401
        # Degraded => 503 even with a valid token.
        client.app.state.degraded = True
        assert client.post("/api/actions/1/restore", headers=AUTH).status_code == 503


# --- freshness: not connected => explicit error, never silent main ----------
def test_restore_refuses_when_source_not_fresh(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        aid = _seed_action(
            db_path, kind="dedupe_close", status="done", initiator="curator",
            instance_from="ghost", url="https://x/y", url_norm="https://x/y",
            session_id_from="s",
        )
        resp = client.post(f"/api/actions/{aid}/restore", headers=AUTH)
        # Never silently substitutes `main`: an unconnected source is a hard error.
        assert resp.status_code == 409


# --- restore twice: dedup by URL, no duplicate ------------------------------
def test_restore_twice_no_duplicate(tmp_path):
    # Small cmd timeout so that IF dedup were removed, the second restore would
    # send an open_tab, get no reply, and fail fast (reddening the guard).
    app = create_app(_settings(tmp_path, cmd_timeout_ms=400))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            aid = _seed_action(
                db_path, kind="dedupe_close", status="done", initiator="curator",
                instance_from="i1", url="https://x/y", url_norm="https://x/y",
                session_id_from="sess-1",
            )

            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(f"/api/actions/{aid}/restore", headers=AUTH)
            )
            cmd = ws.receive_json()
            assert cmd["type"] == "command" and cmd["command"] == "open_tab"
            assert cmd["sessionId"] == "sess-1"     # session stamped
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 99, "windowId": 1}})
            resp1 = fut.result(timeout=5)
            assert resp1.status_code == 200
            assert resp1.json()["restored"] is True

            # Exactly one exemption for the source, with the restore window.
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='i1'"
            ) == (1,)
            assert _db_row(
                db_path, "SELECT reason FROM exemptions WHERE instance_id='i1'"
            ) == ("restore",)
            # One restore action row, original stamped restored_at.
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM actions WHERE kind='restore'"
            ) == (1,)
            assert _db_row(
                db_path, "SELECT restored_at FROM actions WHERE id=?", (aid,)
            )[0] is not None

            # The extension has now opened the tab; a fresh snapshot mirrors it.
            _seed_tab(db_path, "i1", tab_id=99, url="https://x/y")

            # Second restore: dedup by URL => no open, no growth. (Runs inline: a
            # correct dedup path never touches the websocket.)
            resp2 = client.post(f"/api/actions/{aid}/restore", headers=AUTH)
            assert resp2.status_code == 200
            assert resp2.json()["restored"] is False
            # No duplicate exemption, no second restore action.
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='i1'"
            ) == (1,)
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM actions WHERE kind='restore'"
            ) == (1,)
        finally:
            ws.__exit__(None, None, None)


# --- precise relocate targeting (not a url_norm scan) -----------------------
def test_restore_targets_exact_relocate_not_newest_by_url(tmp_path):
    # A COMPLETED past relocation of the same URL from the same source stays
    # status='done', restored_at=NULL forever (§4 has no intermediate status). The
    # cancellation must abandon the RESTORED row's own relocate (by id), never the
    # newest same-URL row a `ORDER BY ts DESC` scan would pick. The decoy is NEWER,
    # so the old scan would have abandoned it — this reddens under the scan bug.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        try:
            real = _seed_action(
                db_path, ts=500_000, kind="relocate", status="done", initiator="curator",
                instance_from="src", instance_to="dst", tab_id=5,
                session_id_from="sess-1", tab_id_to=77, session_id_to="sess-9",
                url="https://a/b", url_norm="https://a/b",
            )
            decoy = _seed_action(  # NEWER completed relocation of the same URL
                db_path, ts=900_000, kind="relocate", status="done", initiator="curator",
                instance_from="src", instance_to="dst", tab_id=1,
                session_id_from="sess-1", tab_id_to=11, session_id_to="sess-9",
                url="https://a/b", url_norm="https://a/b",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(f"/api/actions/{real}/restore", headers=AUTH)
            )
            cmd = ws.receive_json()
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 55, "windowId": 1}})
            assert fut.result(timeout=5).status_code == 200
            # Only the restored relocate is abandoned; the newer decoy is untouched.
            assert _db_row(db_path, "SELECT status FROM actions WHERE id=?", (real,)) == ("abandoned",)
            assert _db_row(db_path, "SELECT status FROM actions WHERE id=?", (decoy,)) == ("done",)
        finally:
            ws.__exit__(None, None, None)


# --- restore of an unfinished relocation => abandoned + both-side exemptions -
def test_restore_unfinished_relocation(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # The source ('src') is the live instance we reopen in; 'dst' holds the
        # copy and need not be connected.
        ws = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        try:
            reloc_id = _seed_action(
                db_path, kind="relocate", status="done", initiator="curator",
                instance_from="src", instance_to="dst",
                tab_id=5, session_id_from="sess-1", tab_id_to=77, session_id_to="sess-9",
                url="https://a/b", url_norm="https://a/b",
            )

            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(f"/api/actions/{reloc_id}/restore", headers=AUTH)
            )
            cmd = ws.receive_json()
            assert cmd["command"] == "open_tab"      # an OPEN, never a close
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 55, "windowId": 1}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            assert resp.json()["restored"] is True

            # The relocate row is abandoned (drop the marking => stays 'done').
            assert _db_row(
                db_path, "SELECT status FROM actions WHERE id=?", (reloc_id,)
            ) == ("abandoned",)
            assert _db_row(
                db_path, "SELECT restored_at FROM actions WHERE id=?", (reloc_id,)
            )[0] is not None
            # Exemptions on BOTH sides (drop the target-side write => this reddens).
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='src'"
            ) == (1,)
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='dst'"
            ) == (1,)
        finally:
            ws.__exit__(None, None, None)
