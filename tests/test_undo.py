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

from conftest import _recv, approve_instance, make_settings, secret_hash_for
from starlette.testclient import TestClient

from src.app import create_app
from src.db.actions import insert_action

EXT_TOKEN = "test-ext-token"
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
    return make_settings(tmp_path, **over)


def _hello(instance_id="src", session="sess-1", **over):
    msg = {
        "type": "hello",
        "protocolVersion": 1,
        "secretHash": secret_hash_for(instance_id),
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
    # Secret-based hello (issue #35): approve the instance (Task E) before it can hello.
    approve_instance(db_path, instance_id)
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    _recv(ws)                # hello_ack
    req = _recv(ws)          # snapshot_request
    ws.send_json(_snapshot(req["id"], tabs or [], session=session))
    # HARD assert, not a best-effort wait: the channel clears ``pending_snapshot_id``
    # BEFORE it writes the snapshot, so "snapshot_at is set" is the proof that the
    # request slot is free again. Letting an unlanded handshake slide made every later
    # step race — ``/api/state``'s kick correctly SKIPS an instance whose slot is still
    # occupied ("a refresh is already in flight"), and the test would then wait forever
    # for a frame that was never going to be sent.
    assert _wait_until(
        lambda: _db_row(
            db_path, "SELECT snapshot_at FROM instances WHERE id=?", (instance_id,)
        )[0]
        is not None
    ), f"instance {instance_id!r} never applied its initial snapshot"
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


# --- a PENDING close is in-flight => undo skips it (Фаза 16, WARNING-1) ------
def test_undo_skips_pending_close(tmp_path):
    """A `pending` relocate_close/dedupe_close is a close IN-FLIGHT (its completion not
    yet journaled), not a completed action. Undo must NOT reverse it: it counts 0 impact
    (so no confirm gate) and is reported as skipped. Reddens if pending were treated as
    a done close (impact>0 => a 409, and it would try to reopen the source)."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_pass(db_path, "p1")
        aid = _seed_action(
            db_path, pass_id="p1", kind="dedupe_close", status="pending", initiator="curator",
            instance_from="ghost", url="https://x/y", url_norm="https://x/y",
            session_id_from="s",
        )
        # impact 0 (pending ignored by _classify) => 200, no confirm required.
        resp = client.post("/api/passes/p1/undo", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []  # NOT reversed
        assert any(s["action_id"] == aid and s["reason"] == "status_pending"
                   for s in body["skipped"])
        # The pending row is untouched (no restored_at stamped).
        assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (aid,))[0] is None


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
            cmd = _recv(ws)
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
            open_cmd = _recv(ws_src)
            assert open_cmd["command"] == "open_tab"
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            # 2) close the copy tab_id_to in instance_to ('dst') WITH step-4 expect.
            close_cmd = _recv(ws_dst)
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
            open_cmd = _recv(ws_src)
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
            cmd = _recv(ws)          # the dedupe_close reopen
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
            open_cmd = _recv(ws_src)
            assert open_cmd["command"] == "open_tab"
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            close_cmd = _recv(ws_dst)
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
            open_cmd = _recv(ws_src)
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            close_cmd = _recv(ws_dst)
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


# --- the copy close is JOURNALED, at-least-once (Фаза 16 discipline) --------
def test_undo_copy_close_is_journaled_pending_then_done(tmp_path):
    """Until now this was the ONLY close in the system performed outside the journal:
    ``close_tab`` followed by a bare ``DELETE FROM tabs``. Фаза 16's at-least-once
    discipline (``pending`` under the guards BEFORE the command, completed after)
    applies here for the same reason — a process death between a successful close and
    the completion write must not erase the evidence that a tab was closed.

    The test freezes the moment the ``close_tab`` is on the wire: the row must already
    exist as ``pending``, and become ``done`` only after the extension answers."""
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
            open_cmd = _recv(ws_src)
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})

            close_cmd = _recv(ws_dst)             # on the wire, NOT answered
            assert close_cmd["command"] == "close_tab"
            # THE assertion: the journal row exists BEFORE the outcome is known.
            row = _db_row(
                db_path,
                "SELECT kind, status, initiator, instance_from, tab_id, "
                "session_id_from, origin_action_id, url FROM actions "
                "WHERE kind='undo_close'",
            )
            assert row == ("undo_close", "pending", "user", "dst", 77, "sess-9",
                           reloc, "https://a/b")
            # It names the COPY's side, not the relocation's source — the tab actually
            # being closed. (Recording it as `relocate_close` would tell the archive the
            # relocation COMPLETED, the opposite of what an undo does.)

            ws_dst.send_json({"type": "response", "id": close_cmd["id"], "ok": True,
                              "result": {}})
            assert fut.result(timeout=5).status_code == 200
            # Completed, and the copy is gone from the mirror.
            assert _db_row(
                db_path, "SELECT status FROM actions WHERE kind='undo_close'"
            ) == ("done",)
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM tabs WHERE instance_id='dst' AND tab_id=77"
            ) == (0,)
        finally:
            ws_dst.__exit__(None, None, None)
            ws_src.__exit__(None, None, None)


def test_undo_copy_close_refused_is_journaled_failed(tmp_path):
    # precondition_failed (the human is using the copy right now) is not silence: the
    # attempt is recorded with the §6 code, and the mirror row survives.
    app = create_app(_settings(tmp_path, cmd_timeout_ms=2000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws_src = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        ws_dst = _connect_fresh(client, db_path, instance_id="dst", session="sess-9", tabs=[])
        try:
            _seed_pass(db_path, "p1")
            _seed_action(
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
            open_cmd = _recv(ws_src)
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            close_cmd = _recv(ws_dst)
            ws_dst.send_json({
                "type": "response", "id": close_cmd["id"], "ok": False,
                "error": {"code": "precondition_failed", "message": "in use"},
            })
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            assert resp.json()["results"][0]["copy_closed"] is False
            assert _db_row(
                db_path, "SELECT status, reason FROM actions WHERE kind='undo_close'"
            ) == ("failed", "precondition_failed")
        finally:
            ws_dst.__exit__(None, None, None)
            ws_src.__exit__(None, None, None)


def test_undo_skipped_copy_close_writes_no_row(tmp_path):
    # The skip branches (no copy / target disconnected / session mismatch) send NO
    # command, so there is nothing to journal — a row there would claim a close that
    # never happened. Reddens if the pending write moves above the guards.
    app = create_app(_settings(tmp_path, cmd_timeout_ms=400))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws_src = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
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
            open_cmd = _recv(ws_src)
            ws_src.send_json({"type": "response", "id": open_cmd["id"], "ok": True,
                              "result": {"tabId": 55, "windowId": 1}})
            resp = fut.result(timeout=5)
            assert resp.json()["results"][0]["copy_reason"] == "session_mismatch"
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM actions WHERE kind='undo_close'"
            ) == (0,)
        finally:
            ws_dst.__exit__(None, None, None)
            ws_src.__exit__(None, None, None)


# --- a failed reopen must not destroy the per-row summary (§10) -------------
def test_undo_survives_a_failed_open_and_still_reports_per_row(tmp_path):
    """§10: "построчный итог" — partial undo is the norm, not a failure.

    A ``CommandError`` from ``open_tab`` used to escape ``restore_row`` (it is not an
    ``HTTPException``, which is all ``_undo_relocation`` catches and all ``create_app``
    renders), so the client got a 500 AFTER some rows had already been reversed —
    ``restored_at`` stamped, copies closed — with no record of what had happened.

    Here the second pure close is answered and the first is left to time out; the
    response must be 200 with one ``failed`` row carrying the §6 code and one undone."""
    app = create_app(_settings(tmp_path, cmd_timeout_ms=400))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws_src = _connect_fresh(client, db_path, instance_id="src", session="sess-1", tabs=[])
        try:
            _seed_pass(db_path, "p1")
            older = _seed_action(
                db_path, ts=1_000, pass_id="p1", kind="dedupe_close", status="done",
                initiator="curator", instance_from="src", url="https://a/1",
                url_norm="https://a/1", session_id_from="sess-1",
            )
            newer = _seed_action(
                db_path, ts=2_000, pass_id="p1", kind="dedupe_close", status="done",
                initiator="curator", instance_from="src", url="https://a/2",
                url_norm="https://a/2", session_id_from="sess-1",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(
                lambda: client.post(
                    "/api/passes/p1/undo", headers=AUTH, json={"confirm_impact": True}
                )
            )
            # Reverse ts order: the NEWER row is reopened first — answer it…
            cmd1 = _recv(ws_src)
            assert cmd1["command"] == "open_tab"
            ws_src.send_json({"type": "response", "id": cmd1["id"], "ok": True,
                              "result": {"tabId": 51, "windowId": 1}})
            # …and let the older one's open_tab time out.
            cmd2 = _recv(ws_src)
            assert cmd2["command"] == "open_tab"

            resp = fut.result(timeout=8)
            assert resp.status_code == 200, "a failed row must not 500 the whole undo"
            body = resp.json()
            by_id = {r["action_id"]: r for r in body["results"]}
            assert by_id[newer]["outcome"] == "undone" and by_id[newer]["reopened"] is True
            assert by_id[older]["outcome"] == "failed"
            # The §6 code is readable in the summary, not a Python repr.
            assert "timeout" in by_id[older]["reason"]
            assert body["counts"]["reopened"] == 1 and body["counts"]["failed"] == 1
            # The successful half really was committed…
            assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (newer,))[0] is not None
            # …and the failed half left neither a marker nor a stray exemption.
            assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (older,))[0] is None
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM exemptions WHERE url='https://a/1'"
            ) == (0,)
        finally:
            ws_src.__exit__(None, None, None)
