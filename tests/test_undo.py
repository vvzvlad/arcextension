"""POST /api/passes/:pass_id/undo (§10) via TestClient (websocket + HTTP).

The websocket(s) act as the extension(s): the source answers its initial
snapshot_request (so it is FRESH) and then the service's ``open_tab``; the target
answers the ``close_tab`` for the copy. The HTTP POST runs on a background thread so
the same test thread can drive the sockets while the request is in flight.

Each guard test is written to REDDEN if its guard is removed (the brief's mutation
checklist): exemption-on-undo, session-mismatch-skips-copy-close,
window_merge-reported-un-undone, confirm-gate, reverse-ts, restored_at-skipped,
origin_action_id-links-both-halves.
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


def _hello(instance_id="src", session="sess-1", **over):
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


def _seed_pass(db_path, pass_id, started_at=1_000_000):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO passes (pass_id, started_at) VALUES (?, ?)", (pass_id, started_at)
        )
        conn.commit()
    finally:
        conn.close()


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
            "self_navigating, audible, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instance_id, tab_id, 1, url, "t", None, 0, 0, 0, 0, 0, 0, 0, 0),
        )
        conn.commit()
    finally:
        conn.close()


def _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=None):
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
def test_undo_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/api/passes/p1/undo").status_code == 401
        _seed_pass(str(tmp_path / "curator.db"), "p1")
        client.app.state.degraded = True
        assert client.post("/api/passes/p1/undo", headers=AUTH).status_code == 503


def test_undo_unknown_pass_404(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/api/passes/nope/undo", headers=AUTH).status_code == 404


# --- confirm gate: impact>0 without confirm_impact => 409 + preview ---------
def test_undo_confirm_gate(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_pass(db_path, "p1")
        _seed_action(
            db_path, pass_id="p1", kind="dedupe_close", status="done", initiator="curator",
            instance_from="src", url="https://x/y", url_norm="https://x/y", session_id_from="sess-1",
        )
        # No confirm_impact and impact>0 => 409 carrying the preview (drop the gate =>
        # this becomes a 200 and reddens).
        resp = client.post("/api/passes/p1/undo", headers=AUTH)
        assert resp.status_code == 409
        body = resp.json()
        assert body["requires_confirm"] is True
        assert body["impact"] > 0
        assert body["error"] == "confirm_impact required"


# --- reverse ts order -------------------------------------------------------
def test_undo_processes_rows_reverse_ts(tmp_path):
    # Two pure closes at different ts, both with a DISCONNECTED source (so each
    # fails fast with no websocket). The result ORDER still proves reverse-ts
    # processing: results[0] must be the NEWER row (flip reverse= => reddens).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_pass(db_path, "p1")
        older = _seed_action(
            db_path, ts=100, pass_id="p1", kind="dedupe_close", status="done",
            initiator="curator", instance_from="ghost", url="https://a/1",
            url_norm="https://a/1", session_id_from="s",
        )
        newer = _seed_action(
            db_path, ts=900, pass_id="p1", kind="dedupe_close", status="done",
            initiator="curator", instance_from="ghost", url="https://a/2",
            url_norm="https://a/2", session_id_from="s",
        )
        resp = client.post(
            "/api/passes/p1/undo", headers=AUTH, json={"confirm_impact": True}
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["action_id"] for r in results] == [newer, older]


# --- restored_at rows are skipped (idempotency marker) ----------------------
def test_undo_skips_restored_at_rows(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_pass(db_path, "p1")
        aid = _seed_action(
            db_path, pass_id="p1", kind="dedupe_close", status="done", initiator="curator",
            instance_from="ghost", url="https://x/y", url_norm="https://x/y",
            session_id_from="s", restored_at=1_500_000,
        )
        # A pass whose only reversible row is already restored => impact 0, no confirm.
        resp = client.post("/api/passes/p1/undo", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        # Skipped, NOT acted on (drop the restored_at skip => it would try to restore
        # via the disconnected 'ghost' and land in results as a failure).
        assert body["results"] == []
        assert any(s["action_id"] == aid and s["reason"] == "already_undone"
                   for s in body["skipped"])


# --- dedupe_close undo: reopen source + write exemption (no re-eviction) ----
def test_undo_pure_close_reopens_and_writes_exemption(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        try:
            _seed_pass(db_path, "p1")
            aid = _seed_action(
                db_path, pass_id="p1", kind="dedupe_close", status="done", initiator="curator",
                instance_from="src", url="https://x/y", url_norm="https://x/y",
                session_id_from="sess-1",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/passes/p1/undo", headers=AUTH, json={"confirm_impact": True}
                )
            )
            cmd = ws.receive_json()
            assert cmd["command"] == "open_tab"     # a reopen BY URL, never a close
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 55, "windowId": 1}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            r0 = resp.json()["results"][0]
            assert r0["action_id"] == aid and r0["reopened"] is True
            # Exemption written on the source (drop the exemption => next pass re-evicts;
            # _record_restore is the shared mechanism, so removing it reddens here).
            assert _db_row(
                db_path, "SELECT reason FROM exemptions WHERE instance_id='src'"
            ) == ("restore",)
            # The original row is stamped restored_at (idempotency marker).
            assert _db_row(
                db_path, "SELECT restored_at FROM actions WHERE id=?", (aid,)
            )[0] is not None
        finally:
            ws.__exit__(None, None, None)


# --- relocate undo: reopen source AND close the copy (both-side exemptions) --
def test_undo_relocate_reopens_source_and_closes_copy(tmp_path):
    app = create_app(_settings(tmp_path, cmd_timeout_ms=2000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws_src = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        ws_dst = _connect_fresh(client, db_path, instance_id="dst", session="sess-9", tabs=[])
        try:
            _seed_pass(db_path, "p1")
            reloc = _seed_action(
                db_path, pass_id="p1", kind="relocate", status="done", initiator="curator",
                instance_from="src", instance_to="dst", tab_id=5, session_id_from="sess-1",
                tab_id_to=77, session_id_to="sess-9", url="https://a/b", url_norm="https://a/b",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/passes/p1/undo", headers=AUTH, json={"confirm_impact": True}
                )
            )
            # 1) reopen the source in instance_from ('src').
            open_cmd = ws_src.receive_json()
            assert open_cmd["command"] == "open_tab"
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            # 2) close the copy tab_id_to in instance_to ('dst') WITH step-4 expect.
            close_cmd = ws_dst.receive_json()
            assert close_cmd["command"] == "close_tab"
            assert close_cmd["params"]["tabId"] == 77
            assert close_cmd["params"]["expect"]["minIdleMs"] > 0  # step-4 guard carried
            ws_dst.send_json({"type": "response", "id": close_cmd["id"], "ok": True, "result": {}})

            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            r0 = resp.json()["results"][0]
            assert r0["kind"] == "relocate" and r0["reopened"] is True and r0["copy_closed"] is True
            # relocate row abandoned + restored_at; exemptions on BOTH sides.
            assert _db_row(db_path, "SELECT status FROM actions WHERE id=?", (reloc,)) == ("abandoned",)
            assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (reloc,))[0] is not None
            assert _db_row(db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='src'") == (1,)
            assert _db_row(db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='dst'") == (1,)
        finally:
            ws_dst.__exit__(None, None, None)
            ws_src.__exit__(None, None, None)


# --- session mismatch: reopen by URL, SKIP closing the copy by tab_id -------
def test_undo_after_restart_does_not_close_foreign_copy(tmp_path):
    # The target reconnected under a NEW session, so tab_id_to=77 now addresses a
    # FOREIGN tab. Undo must reopen the source by URL but SKIP the copy close (§5/§10).
    # A small cmd_timeout makes the mutation (drop the session guard) resolve to a
    # 'timeout' reason instead of 'session_mismatch' — a detectable difference.
    app = create_app(_settings(tmp_path, cmd_timeout_ms=400))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws_src = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        # dst is live but under a DIFFERENT session than session_id_to='sess-OLD'.
        ws_dst = _connect_fresh(client, db_path, instance_id="dst", session="sess-NEW", tabs=[])
        try:
            _seed_pass(db_path, "p1")
            _seed_action(
                db_path, pass_id="p1", kind="relocate", status="done", initiator="curator",
                instance_from="src", instance_to="dst", tab_id=5, session_id_from="sess-1",
                tab_id_to=77, session_id_to="sess-OLD", url="https://a/b", url_norm="https://a/b",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/passes/p1/undo", headers=AUTH, json={"confirm_impact": True}
                )
            )
            open_cmd = ws_src.receive_json()
            assert open_cmd["command"] == "open_tab"     # reopen by URL still happens
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            r0 = resp.json()["results"][0]
            assert r0["reopened"] is True
            # The copy close was SKIPPED for a session mismatch — no foreign tab touched.
            assert r0["copy_closed"] is False
            assert r0["copy_reason"] == "session_mismatch"
        finally:
            ws_dst.__exit__(None, None, None)
            ws_src.__exit__(None, None, None)


# --- window_merge is reported un-undone; the rest is undone ------------------
def test_undo_window_merge_reports_un_undone(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        try:
            _seed_pass(db_path, "p1")
            wm = _seed_action(
                db_path, ts=200, pass_id="p1", kind="window_merge", status="done",
                initiator="curator", instance_from="src", url="https://w/m", url_norm="https://w/m",
            )
            dc = _seed_action(
                db_path, ts=100, pass_id="p1", kind="dedupe_close", status="done",
                initiator="curator", instance_from="src", url="https://x/y",
                url_norm="https://x/y", session_id_from="sess-1",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/passes/p1/undo", headers=AUTH, json={"confirm_impact": True}
                )
            )
            cmd = ws.receive_json()          # the dedupe_close reopen
            assert cmd["command"] == "open_tab"
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 55, "windowId": 1}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            body = resp.json()
            # window_merge honestly reported as un-undone (drop the branch => it lands in
            # `skipped` instead, and this assertion reddens).
            assert any(u["action_id"] == wm and u["reason"] == "window_merge_not_undoable"
                       for u in body["un_undone"])
            assert not any(s["action_id"] == wm for s in body["skipped"])
            # Everything else is still undone (partial undo is normal, not a failure).
            assert any(r["action_id"] == dc and r["reopened"] is True for r in body["results"])
        finally:
            ws.__exit__(None, None, None)


# --- origin_action_id links both halves across passes -----------------------
def test_undo_relocate_close_finds_phase_a_in_another_pass(tmp_path):
    # Phase A (relocate) is in pass 'pA'; phase B (relocate_close) is in pass 'pB'
    # carrying origin_action_id -> the relocate. Undoing pB must FIND the phase-A row,
    # reopen the source, close its copy, and mark BOTH halves restored_at (§10:
    # "undo either finds the other"). Break the origin lookup => the phase-A relocate
    # is never abandoned and this reddens.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws_src = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        ws_dst = _connect_fresh(client, db_path, instance_id="dst", session="sess-9", tabs=[])
        try:
            _seed_pass(db_path, "pA", started_at=100)
            _seed_pass(db_path, "pB", started_at=200)
            reloc = _seed_action(
                db_path, ts=100, pass_id="pA", kind="relocate", status="done", initiator="curator",
                instance_from="src", instance_to="dst", tab_id=5, session_id_from="sess-1",
                tab_id_to=77, session_id_to="sess-9", url="https://a/b", url_norm="https://a/b",
            )
            close = _seed_action(
                db_path, ts=200, pass_id="pB", kind="relocate_close", status="done",
                initiator="curator", origin_action_id=reloc, instance_from="src", instance_to="dst",
                tab_id=5, session_id_from="sess-1", tab_id_to=77, session_id_to="sess-9",
                url="https://a/b", url_norm="https://a/b",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/passes/pB/undo", headers=AUTH, json={"confirm_impact": True}
                )
            )
            open_cmd = ws_src.receive_json()
            assert open_cmd["command"] == "open_tab"
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            close_cmd = ws_dst.receive_json()
            assert close_cmd["command"] == "close_tab" and close_cmd["params"]["tabId"] == 77
            ws_dst.send_json({"type": "response", "id": close_cmd["id"], "ok": True, "result": {}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            r0 = resp.json()["results"][0]
            assert r0["relocate_id"] == reloc and r0["reopened"] is True and r0["copy_closed"] is True
            # The phase-A relocate (in the OTHER pass) is found and abandoned+restored.
            assert _db_row(db_path, "SELECT status FROM actions WHERE id=?", (reloc,)) == ("abandoned",)
            assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (reloc,))[0] is not None
            # The phase-B close is marked restored_at too (undoing pA later skips it).
            assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (close,))[0] is not None
        finally:
            ws_dst.__exit__(None, None, None)
            ws_src.__exit__(None, None, None)


# --- both halves in ONE pass: the relocation is reversed exactly once --------
def test_undo_pass_with_both_halves_reverses_once(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws_src = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        ws_dst = _connect_fresh(client, db_path, instance_id="dst", session="sess-9", tabs=[])
        try:
            _seed_pass(db_path, "p1")
            reloc = _seed_action(
                db_path, ts=100, pass_id="p1", kind="relocate", status="done", initiator="curator",
                instance_from="src", instance_to="dst", tab_id=5, session_id_from="sess-1",
                tab_id_to=77, session_id_to="sess-9", url="https://a/b", url_norm="https://a/b",
            )
            close = _seed_action(
                db_path, ts=200, pass_id="p1", kind="relocate_close", status="done",
                initiator="curator", origin_action_id=reloc, instance_from="src", instance_to="dst",
                tab_id=5, session_id_from="sess-1", tab_id_to=77, session_id_to="sess-9",
                url="https://a/b", url_norm="https://a/b",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/passes/p1/undo", headers=AUTH, json={"confirm_impact": True}
                )
            )
            open_cmd = ws_src.receive_json()
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            close_cmd = ws_dst.receive_json()
            ws_dst.send_json({"type": "response", "id": close_cmd["id"], "ok": True, "result": {}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            body = resp.json()
            # Reversed once (one reopen, one copy close); the paired half is skipped.
            assert body["counts"]["reopened"] == 1
            assert body["counts"]["copies_closed"] == 1
            assert any(s["reason"] == "paired_already_undone" for s in body["skipped"])
            assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (close,))[0] is not None
        finally:
            ws_dst.__exit__(None, None, None)
            ws_src.__exit__(None, None, None)
