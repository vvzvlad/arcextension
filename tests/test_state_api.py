"""GET /api/state + POST /api/focus (§10), plus the single-flight refresh guard.

The websocket acts as the extension exactly as in test_restore: it answers the
initial snapshot_request so the instance is fresh, and answers /api/focus's
``focus_tab`` command. The HTTP call runs on a background thread so the same test
thread can drive the websocket while the request is in flight.
"""

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from conftest import _recv, approve_instance, make_settings, secret_for
from starlette.testclient import TestClient

from src.api.state import kick_state_refresh
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
    return make_settings(tmp_path, **over)


def _hello(instance_id="i1", session="sess-1", **over):
    msg = {
        "type": "hello",
        "protocolVersion": 1,
        "secret": secret_for(instance_id),
        "instanceId": instance_id,
        "installUuid": "uuid-A",
        "origin": "chrome-extension://abc",
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
    # Secret-based hello (issue #35): approve the instance (Task E) before it can hello.
    approve_instance(db_path, instance_id)
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    _recv(ws)                 # hello_ack
    req = _recv(ws)           # snapshot_request
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


def test_revoked_instance_and_its_tabs_leave_the_state_mirror(tmp_path):
    """``/api/state`` is the CURATED fleet, so a revoked instance leaves it — with its
    tabs.

    The same status filter ``known_instance_ids`` and ``load_preview_input`` already apply
    (issue #35 §6: "in both places or neither"), extended to the two read surfaces that
    still lacked it. For the startpage a revoked instance is dead weight in every sense:
    its socket is closed, a jump to its tabs can only fail, its mirror can never refresh
    again, and the row would sit in the status strip reading "offline for N days" with no
    way for the human to clear it — retired instances are administered in
    ``/admin/instances``, which lists every status on purpose.

    The tabs go with it: nothing ever deletes a revoked instance's tabs (``apply_snapshot``
    is their only writer and it needs a live socket), so leaving them behind would render a
    phantom group whose "jump" can only fail. Reddens if either filter is dropped.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(
            client, db_path,
            tabs=[{"tabId": 7, "windowId": 1, "url": "https://a/b", "title": "A"}],
        )
        try:
            body = client.get("/api/state", headers=AUTH).json()
            assert [i["id"] for i in body["instances"]] == ["i1"]
            assert [t["tab_id"] for t in body["tabs"]] == [7]

            # Revoke it exactly as the /admin handler's transaction does.
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("UPDATE instances SET status='revoked' WHERE id='i1'")
                conn.commit()
            finally:
                conn.close()

            after = client.get("/api/state", headers=AUTH).json()
            assert after["instances"] == []
            assert after["tabs"] == [], "a retired instance's tabs became a phantom group"
            # The row itself is NOT deleted — it is still there for /admin/instances.
            assert _db_row(db_path, "SELECT status FROM instances WHERE id='i1'") == (
                "revoked",
            )
        finally:
            ws.__exit__(None, None, None)


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
                "rules_invalid", "paused_until", "resume_pending", "pending_plan",
                "instances", "tabs", "quick_links",
            }
            # Not paused / not waiting for a click on a fresh mirror (§7); with no
            # latch armed the deferred plan is null, not a stray object.
            assert body["paused_until"] is None and body["resume_pending"] is False
            assert body["pending_plan"] is None
            assert isinstance(body["server_now"], int)
            assert body["rules_total"] == 0 and body["rules_invalid"] == 0
            # The instance is present with the §10 fields.
            inst = {i["id"]: i for i in body["instances"]}["i1"]
            assert inst["connected"] is True
            # No `title`: the id IS the name (§6) and the column is gone (migration 3).
            assert set(inst.keys()) == {
                "id", "connected", "snapshot_at", "last_seen_at",
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
def _stale_the_mirror(db_path, instance_id="i1"):
    """Age the mirror out by REWRITING ``snapshot_at``, not by waiting.

    The old form asked for staleness with ``state_fresh_ms=1`` and let real time supply
    the millisecond. It usually did — but the whole handshake→GET path can complete
    inside one millisecond, and then ``_is_stale`` compares ``now - snapshot_at == 0``
    against ``>= 1``, the kick correctly declines, and the test fails on timing rather
    than on behaviour. Writing the timestamp makes "the mirror is old" a fact.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("UPDATE instances SET snapshot_at = 0 WHERE id = ?", (instance_id,))
        conn.commit()
    finally:
        conn.close()


def test_state_kicks_background_refresh_when_stale(tmp_path):
    # A mirror older than STATE_FRESH_MS must make GET /api/state kick a background
    # refresh: the ws receives a NEW snapshot_request. snapshot_timeout small so the
    # detached poll-task dies quickly. If the refresh were removed, no second
    # snapshot_request would arrive and `_recv` fails within its deadline.
    app = create_app(_settings(tmp_path, state_fresh_ms=3000, snapshot_timeout_ms=300))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            _stale_the_mirror(db_path)
            resp = client.get("/api/state", headers=AUTH)
            assert resp.status_code == 200
            # The background task (after the response) kicked a refresh: a fresh
            # snapshot_request is now waiting on the socket.
            frame = _recv(ws)
            assert frame["type"] == "snapshot_request"
        finally:
            ws.__exit__(None, None, None)


def test_state_does_not_await_a_snapshot_even_with_a_large_timeout(tmp_path):
    """Regression (issue #44 acceptance d): ``list_tabs`` / ``list_instances`` now BLOCK on
    a fresh snapshot (§6), but ``/api/state`` deliberately does NOT — its refresh stays a
    detached background kick, because the startpage is newtab and a blocking fan-out would
    move ``snapshot_at`` on every Cmd+T during a pass.

    Proven by construction, not luck: with a stale mirror and a 5 s snapshot timeout, a
    blocking implementation would await the answer (that we never send) for ~5 s. The
    detached one returns in well under that. The generous margin keeps this off timing
    flakiness while still failing hard if ``/api/state`` were ever made to await.
    """
    app = create_app(_settings(tmp_path, state_fresh_ms=3000, snapshot_timeout_ms=5000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            _stale_the_mirror(db_path)   # a refresh is genuinely wanted (by fact, not timing)
            started = time.time()
            resp = client.get("/api/state", headers=AUTH)   # we never answer the kicked request
            elapsed = time.time() - started
            assert resp.status_code == 200
            # Nowhere near the 5 s a blocking await would have cost.
            assert elapsed < 2.0, f"/api/state appears to await the snapshot ({elapsed:.2f}s)"
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
            cmd = _recv(ws)
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
            cmd = _recv(ws)
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


def test_state_refresh_does_not_clobber_a_pending_pass_snapshot():
    """A newtab opened during a curator pass's readiness window fires the kick while
    the pass holds ``pending_snapshot_id`` (a ``pass-<uuid>``). The kick must NOT
    overwrite it and must send NO competing snapshot_request for that instance —
    else the instance's answer to the pass id is dropped and it is silently excluded
    from the pass (restore.py:124-128). Removing the ``pending_snapshot_id`` guard in
    ``kick_state_refresh`` reddens this (the pass id gets clobbered by a ``req-``)."""

    async def scenario():
        cs = _FakeConnState("sess-1")
        # A curator pass already sent its snapshot_request and is awaiting the answer.
        cs.pending_snapshot_id = "pass-abc"
        registry = _FakeRegistry({"i1": cs})
        db = _FakeDb(_FakeConn("i1", "sess-1", snapshot_at=0))  # stale mirror
        app = SimpleNamespace(
            state=SimpleNamespace(ext_registry=registry, state_refresh_tasks=set())
        )
        settings = SimpleNamespace(state_fresh_ms=1000, snapshot_timeout_ms=100)

        await kick_state_refresh(app, db, settings)
        # Drain any detached clear-tasks (there should be none, but be defensive).
        tasks = list(app.state.state_refresh_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        return cs

    cs = asyncio.run(scenario())
    # The pass's pending id survived untouched — the instance stays in the pass.
    assert cs.pending_snapshot_id == "pass-abc"
    # And no competing snapshot_request was sent for it.
    requests = [f for f in cs.ws.sent if f.get("type") == "snapshot_request"]
    assert requests == []
    # The single-flight flag was never claimed (the kick skipped the instance).
    assert cs.state_refresh_inflight is False


# --- the deferred plan reaches the client (§7) ------------------------------
def _set_setting(db_path, key, value):
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


def test_state_carries_the_pending_plan_not_just_the_flag(tmp_path):
    """§7: after a timeout expiry the runner stores ``{since, plan}`` and the plan «выводится
    в статус-полосу» — the human confirms the burst SEEING what it will do. Reducing the
    latch to a boolean threw exactly that away. ``resume_pending`` stays a bool (clients
    are built on it) and ``pending_plan`` is additive next to it."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        body = client.get("/api/state", headers=AUTH).json()
        assert body["resume_pending"] is False and body["pending_plan"] is None

        _set_setting(db_path, "resume_pending", json.dumps({
            "since": 1_700_000_000_000,
            "plan": {"relocations": 2, "closures": 7, "deferred": {},
                     "closure_examples": [{"url": "https://a/b", "instance": "main",
                                           "kind": "dedupe_close"}]},
        }))
        body = client.get("/api/state", headers=AUTH).json()
        # The boolean is untouched…
        assert body["resume_pending"] is True
        # …and the plan is there, whole: the status row can name the numbers and show
        # the examples instead of a bare "something is pending".
        assert body["pending_plan"]["since"] == 1_700_000_000_000
        assert body["pending_plan"]["plan"]["closures"] == 7
        assert body["pending_plan"]["plan"]["closure_examples"][0]["url"] == "https://a/b"


def test_state_pending_plan_degrades_to_null_on_a_malformed_latch(tmp_path):
    # A corrupt latch must not 500 every /api/state — the flag still says "waiting",
    # the plan degrades to null.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _set_setting(db_path, "resume_pending", "{not json")
        body = client.get("/api/state", headers=AUTH).json()
        assert body["resume_pending"] is True
        assert body["pending_plan"] is None

        _set_setting(db_path, "resume_pending", "[1, 2]")   # valid JSON, wrong shape
        assert client.get("/api/state", headers=AUTH).json()["pending_plan"] is None


# --- the kick and the "do not clobber" guard, pinned DETERMINISTICALLY -------
# These two reproduce, by construction rather than by timing luck, the interleaving that
# used to make this file hang for minutes. No sleeps, no load, no retries: the state is
# frozen at the exact instant that matters and asserted.
def test_state_kick_respects_an_in_flight_pass_request_and_still_refreshes(tmp_path):
    """A pass is collecting snapshots when the newtab opens (§7/§10).

    ``/api/state`` must NOT put a competing ``req-`` id in the slot — the channel matches
    snapshot ids exactly, so clobbering a ``pass-`` id drops the instance's answer to the
    PASS and ejects it silently. The refresh the page wanted is the one ALREADY in
    flight, so the correct behaviour is: skip the send, and let THAT request's answer be
    the refresh. Both halves are asserted here, which is what "the kick and the guard
    coexist" means.
    """
    app = create_app(_settings(tmp_path, state_fresh_ms=3000, snapshot_timeout_ms=300))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, tabs=[])
        try:
            _stale_the_mirror(db_path)   # a refresh is genuinely wanted, by fact not timing
            cs = client.app.state.ext_registry.get("i1")
            # Freeze the interleaving instead of hoping for it: the pass's request sits
            # in the slot at the exact moment /api/state runs its kick.
            cs.pending_snapshot_id = "pass-frozen"
            cs.pending_sent_at = int(time.time() * 1000)
            assert _db_row(db_path, "SELECT COUNT(*) FROM tabs")[0] == 0

            assert client.get("/api/state", headers=AUTH).status_code == 200
            # (a) the slot is untouched => the instance stays in the pass.
            assert cs.pending_snapshot_id == "pass-frozen"

            # (b) answering the IN-FLIGHT request is what refreshes the mirror — the
            # page never needed a second one. (Asserted on the tab that arrives, not on
            # ``snapshot_at``: the handshake and this reply can share a millisecond.)
            ws.send_json(_snapshot("pass-frozen", [
                {"tabId": 3, "windowId": 1, "url": "https://x/y", "title": "X"}
            ]))
            assert _wait_until(
                lambda: _db_row(db_path, "SELECT COUNT(*) FROM tabs")[0] == 1
            ), "the in-flight snapshot never landed"
            assert cs.last_applied_snapshot_id == "pass-frozen"
            body = client.get("/api/state", headers=AUTH).json()
            assert [t["tab_id"] for t in body["tabs"]] == [3]
        finally:
            ws.__exit__(None, None, None)


def test_an_unlanded_handshake_leaves_the_slot_occupied_and_is_caught(tmp_path):
    """WHY ``_connect_fresh`` now HARD-asserts that the initial snapshot landed.

    If it does not land, the request slot stays occupied; ``/api/state``'s kick then
    CORRECTLY skips the instance ("a refresh is already in flight") and no frame is ever
    sent. A test waiting for that frame used to wait forever — long enough, past
    ``PASS_INTERVAL_MIN``, for the app's real curator driver to wake up inside it. The
    whole chain is reproduced here deterministically by answering with a foreign id,
    which §6 drops by design.
    """
    app = create_app(_settings(tmp_path, state_fresh_ms=3000, snapshot_timeout_ms=200))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")  # secret-hello needs an approved active row (#35)
        ws = client.websocket_connect("/ext").__enter__()
        try:
            ws.send_json(_hello())
            _recv(ws)                       # hello_ack
            _recv(ws)                       # snapshot_request (its id is DISCARDED)
            ws.send_json(_snapshot("req-a-foreign-id", []))

            cs = client.app.state.ext_registry.get("i1")
            # The foreign reply is dropped: the mirror stays empty and the slot occupied.
            assert _db_row(db_path, "SELECT snapshot_at FROM instances WHERE id='i1'")[0] is None
            assert cs.pending_snapshot_id is not None

            # …so the kick sends nothing, and the wait for a frame now FAILS FAST with a
            # readable message instead of hanging (bounded by `_recv`).
            assert client.get("/api/state", headers=AUTH).status_code == 200
            with pytest.raises(AssertionError, match="no websocket frame"):
                _recv(ws, timeout=0.5)
        finally:
            ws.__exit__(None, None, None)


def test_focus_reports_the_missing_field_not_a_body_error(tmp_path):
    # `{}` is a well-formed JSON object that merely lacks the fields, so it must fail
    # like `{"instance": ""}` — 422 naming what is missing — not 400 "request body must
    # be JSON". Malformed JSON is still 400.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/api/focus", headers=AUTH, json={}).status_code == 422
        assert client.post("/api/focus", headers=AUTH).status_code == 422
        assert client.post(
            "/api/focus", headers=AUTH, json={"instance": "i1"}
        ).status_code == 422
        bad = client.post(
            "/api/focus",
            headers={**AUTH, "content-type": "application/json"},
            content=b"{not json",
        )
        assert bad.status_code == 400
