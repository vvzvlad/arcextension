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

from conftest import _recv, approve_instance, make_settings, secret_hash_for
from starlette.testclient import TestClient

from src.app import create_app
from src.db.actions import insert_action

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
    return make_settings(tmp_path, **over)


def _hello(instance_id="i1", session="sess-1", **over):
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
    # Secret-based hello (issue #35): the instance must be an APPROVED active row first
    # (what Task E does), else the hello resolves to no instance and is rejected.
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
def test_restore_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # No token / wrong token => 401 (before any work).
        assert client.post("/api/actions/1/restore").status_code == 401
        assert client.post(
            "/api/actions/1/restore", headers={"Authorization": "Bearer nope"}
        ).status_code == 401
        # A non-ASCII bearer token must be a flat 401, not a 500 (compare_digest
        # raises TypeError on the non-ASCII str Starlette decodes latin-1; the guard
        # compares bytes now). Sent as RAW BYTES — that is the only way a byte >=0x80
        # reaches the app (httpx ASCII-encodes str header values).
        assert client.post(
            "/api/actions/1/restore", headers={"Authorization": b"Bearer br\xe9k\xe9n"}
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


# --- stale mirror: re-snapshot then 409 (never silently main) ---------------
def test_restore_refuses_stale_mirror_not_refreshed(tmp_path):
    # connected but the mirror is stale AND the instance does not answer the
    # re-snapshot => 409, exercising the poll-then-409 half of _ensure_fresh
    # (the load-bearing "never silently main for a live-but-stale mirror").
    app = create_app(_settings(tmp_path, state_fresh_ms=3000, snapshot_timeout_ms=300))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        # Age the mirror out by FACT, not by hoping a millisecond elapses: with
        # `state_fresh_ms=1` the whole handshake can land inside one tick, the mirror
        # reads fresh, and restore proceeds instead of refusing.
        _stale_the_mirror(db_path, 'i1')
        try:
            aid = _seed_action(
                db_path, kind="dedupe_close", status="done", initiator="curator",
                instance_from="i1", url="https://x/y", url_norm="https://x/y",
                session_id_from="sess-1",
            )
            # Do NOT answer the snapshot_request the endpoint emits => it times out.
            resp = client.post(f"/api/actions/{aid}/restore", headers=AUTH)
            assert resp.status_code == 409
        finally:
            ws.__exit__(None, None, None)


# NB: the proceed half (stale mirror answered fresh -> open_tab) is exercised by
# the fresh-mirror restore tests below (they prove the open_tab continuation), and
# the 409 test above proves the stale-recheck GATE. A dedicated HTTP+ws concurrent
# test of the proceed path deadlocks TestClient's single-portal threading, so it is
# intentionally omitted rather than made flaky.


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
            cmd = _recv(ws)
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
            cmd = _recv(ws)
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
            cmd = _recv(ws)
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


# --- exemptions land BEFORE the open (§10 "in one transaction with the open") -
def test_exemptions_are_written_before_the_open_tab_command(tmp_path):
    """§10 asks for the exemption "in one transaction with the open". Literally that is
    impossible — the open is a WS command outside any transaction — so the ORDER has to
    fail safe. With the write after the open, a crash in between leaves a reopened tab
    with no exemption, no ``restore`` row and no ``restored_at``, and the very next pass
    evicts it again ("restore breaks the tab", §10).

    The test freezes the moment the ``open_tab`` command is on the wire, BEFORE any
    response, and reads the DB: both sides' exemptions must already be committed.
    Reddens if the exemption write moves back after the command."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
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
            cmd = _recv(ws)                  # the open_tab is out; NOT answered
            assert cmd["command"] == "open_tab"
            # THE assertion: the protection exists already, on BOTH sides, while the
            # command is still in flight — this is the window a crash would land in.
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='src'"
            ) == (1,)
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM exemptions WHERE instance_id='dst'"
            ) == (1,)
            # The relocate row is NOT yet abandoned: only the exemptions moved earlier,
            # the rest of the semantics still belongs to the completed restore.
            assert _db_row(
                db_path, "SELECT status FROM actions WHERE id=?", (reloc_id,)
            ) == ("done",)

            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 55, "windowId": 1}})
            assert fut.result(timeout=5).status_code == 200
            # And the completed restore still does everything it did before.
            assert _db_row(
                db_path, "SELECT status FROM actions WHERE id=?", (reloc_id,)
            ) == ("abandoned",)
            assert _db_row(
                db_path, "SELECT COUNT(*) FROM actions WHERE kind='restore'"
            ) == (1,)
        finally:
            ws.__exit__(None, None, None)


# --- restore never ejects an instance from a running pass -------------------
def _stale_the_mirror(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("UPDATE instances SET snapshot_at = 0 WHERE id = ?", (instance_id,))
        conn.commit()
    finally:
        conn.close()


def test_restore_waits_for_a_pass_snapshot_instead_of_clobbering_it(tmp_path):
    """A restore clicked WHILE a curator pass is collecting snapshots must not
    overwrite the pass's ``pending_snapshot_id``. The channel matches ids exactly, so
    an overwrite drops the instance's answer to the pass and ejects it silently.

    Reddens both ways if the guard is removed: ``pending_snapshot_id`` is a ``req-``
    at the mid-flight assertion, AND the ``pass-abc`` snapshot below is then rejected
    by the channel, so ``last_applied_snapshot_id`` never becomes ``pass-abc`` and the
    restore 409s instead of proceeding."""
    app = create_app(_settings(tmp_path, state_fresh_ms=5_000, snapshot_timeout_ms=4_000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            _stale_the_mirror(db_path, "i1")
            cs = client.app.state.ext_registry.get("i1")
            # A pass has its own request in flight and is awaiting exactly this id.
            cs.pending_snapshot_id = "pass-abc"
            cs.pending_sent_at = int(time.time() * 1000)

            aid = _seed_action(
                db_path, kind="dedupe_close", status="done", initiator="curator",
                instance_from="i1", url="https://x/y", url_norm="https://x/y",
                session_id_from="sess-1",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(lambda: client.post(f"/api/actions/{aid}/restore", headers=AUTH))
            time.sleep(0.4)
            assert cs.pending_snapshot_id == "pass-abc", "restore clobbered the pass slot"

            # The instance answers the PASS's id: it stays in the pass, and the mirror
            # the restore was waiting for is now fresh.
            ws.send_json(_snapshot("pass-abc", [], session="sess-1"))
            cmd = _recv(ws)
            assert cmd["command"] == "open_tab"     # restore proceeded off that mirror
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 42, "windowId": 1}})
            assert fut.result(timeout=5).status_code == 200
            # The pass's readiness key was set by the instance's own answer.
            assert cs.last_applied_snapshot_id == "pass-abc"
        finally:
            ws.__exit__(None, None, None)


# --- open_tab FAILS: a §6 outcome, not a 500, and no exemption left behind ---
def test_failed_open_is_a_clean_error_and_rolls_the_exemption_back(tmp_path):
    """The extension is wedged: ``open_tab`` times out.

    Two things must not happen. (1) A 500 — ``CommandError`` is not an ``HTTPException``
    and ``create_app`` only renders the latter, so it used to escape as an unhandled
    error. (2) A two-hour exemption earned by an action that never took place: the
    exemption is written BEFORE the open (deliberately, so a CRASH cannot leave a
    reopened tab unprotected), and a normal command failure is the other case — the tab
    is not back, so the protection must not stand. Otherwise "restore failed" silently
    takes that URL out of curation for ``RESTORE_EXEMPTION_MIN``.
    """
    app = create_app(_settings(tmp_path, cmd_timeout_ms=300))
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
            fut = pool.submit(lambda: client.post(f"/api/actions/{aid}/restore", headers=AUTH))
            cmd = _recv(ws)
            assert cmd["command"] == "open_tab"     # sent, and deliberately NOT answered
            resp = fut.result(timeout=5)

            assert resp.status_code == 502          # transport-class, not a 500
            assert resp.json()["error"] == "timeout"
            # No protection was left behind, and nothing was journaled as done.
            assert _db_row(db_path, "SELECT COUNT(*) FROM exemptions") == (0,)
            assert _db_row(db_path, "SELECT COUNT(*) FROM actions WHERE kind='restore'") == (0,)
            assert _db_row(db_path, "SELECT restored_at FROM actions WHERE id=?", (aid,))[0] is None
        finally:
            ws.__exit__(None, None, None)


def test_failed_open_keeps_a_pre_existing_exemption(tmp_path):
    # The rollback RESTORES the prior row rather than deleting: a still-valid exemption
    # written by an earlier restore must survive this attempt's failure. Reddens if the
    # rollback becomes a blanket DELETE.
    app = create_app(_settings(tmp_path, cmd_timeout_ms=300))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                "INSERT INTO exemptions (instance_id, url, until, reason) "
                "VALUES ('i1', 'https://x/y', 999999999999, 'earlier')"
            )
            conn.commit()
            conn.close()

            aid = _seed_action(
                db_path, kind="dedupe_close", status="done", initiator="curator",
                instance_from="i1", url="https://x/y", url_norm="https://x/y",
                session_id_from="sess-1",
            )
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(lambda: client.post(f"/api/actions/{aid}/restore", headers=AUTH))
            _recv(ws)                                # open_tab, never answered
            assert fut.result(timeout=5).status_code == 502
            # Exactly as it was before the failed attempt.
            assert _db_row(
                db_path, "SELECT until, reason FROM exemptions WHERE instance_id='i1'"
            ) == (999999999999, "earlier")
        finally:
            ws.__exit__(None, None, None)


def test_refused_open_is_a_409_with_the_edge_code(tmp_path):
    # A §6 refusal from the edge is "your picture is stale", not a transport fault.
    app = create_app(_settings(tmp_path, cmd_timeout_ms=2000))
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
            fut = pool.submit(lambda: client.post(f"/api/actions/{aid}/restore", headers=AUTH))
            cmd = _recv(ws)
            ws.send_json({"type": "response", "id": cmd["id"], "ok": False,
                          "error": {"code": "precondition_failed", "message": "nope"}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 409
            assert resp.json()["error"] == "precondition_failed"
            assert resp.json()["refetch"] is True
            assert _db_row(db_path, "SELECT COUNT(*) FROM exemptions") == (0,)
        finally:
            ws.__exit__(None, None, None)
