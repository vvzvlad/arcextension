"""Stop/start WRITE side HTTP tests (§7 «Пауза» → the indefinite emergency stop).

Drives the real endpoints through a TestClient:

* ``POST /api/pause`` stops the curator INDEFINITELY and is idempotent on the stop
  time (a re-press keeps the original moment),
* the all-mutation gate answers ``423 {error:"stopped", since}`` on every mutating
  ``/api/*`` verb while stopped — but NOT on reads, ``rules/preview``, the stop/start
  verbs or ``run_pass{dry_run}``,
* ``DELETE /api/pause`` starts (clears the stop), shifts the TTL protections by the
  ACTUAL stop duration, and triggers a pass immediately — NORMAL-gated when it lifts
  a real stop, the latch-confirm click when no stop was set (see ``resume_now``),
* ``run_pass{confirm_pending}`` still refuses under a live stop,

plus the §12 alert tie-in as a pure-function check: overdue is suppressed WHILE
stopped and grows on the same data once running.
"""

from conftest import (
    _recv,
    admin_headers,
    approve_instance,
    instance_headers,
    make_settings,
    secret_for,
    secret_hash_for,
)
from starlette.testclient import TestClient

from src.api.metrics import Snapshot, _pass_overdue_seconds
from src.app import create_app

ADMIN_TOKEN = "test-admin-token"
# /api/* accepts either an admin (ADMIN_TOKEN) or an active-instance secret (issue #35 §4).
# The generic tests here just need a valid caller, so they use the admin credential;
# the force tests that must EXECUTE a forced verb switch to an instance secret.
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``."""
    return make_settings(tmp_path, **{**{
            "cmd_timeout_ms": 1000,
            "pass_interval_min": 5,
            "snapshot_timeout_ms": 200,
            "state_fresh_ms": 3000,
        }, **over})


def _q(db_path, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _w(db_path, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _setting(db_path, key):
    row = _q(db_path, "SELECT value FROM settings WHERE key = ?", (key,))
    return row[0][0] if row else None


def _set_setting(db_path, key, value):
    _w(db_path, "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _passes(db_path):
    return _q(db_path, "SELECT COUNT(*) FROM passes")[0][0]


# --- POST /api/pause: stops indefinitely, idempotent on the timestamp --------
def test_post_pause_stops_indefinitely_and_repress_keeps_the_moment(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        r1 = client.post("/api/pause", headers=AUTH)
        assert r1.status_code == 200
        stopped_at = r1.json()["stopped_at"]
        assert isinstance(stopped_at, int) and stopped_at > 0
        assert _setting(db_path, "curator_stopped_at") == str(stopped_at)

        # A re-press keeps the ORIGINAL stop moment: the start-side TTL shift counts
        # the FULL stop duration, not the time since the last nervous press (§7).
        r2 = client.post("/api/pause", headers=AUTH)
        assert r2.json()["stopped_at"] == stopped_at
        assert _setting(db_path, "curator_stopped_at") == str(stopped_at)

        # There is no duration anymore; a body is ignored, never validated.
        r3 = client.post("/api/pause", headers=AUTH, json={"minutes": 10**9})
        assert r3.status_code == 200 and r3.json()["stopped_at"] == stopped_at


# --- the all-mutation gate --------------------------------------------------
def test_stop_gate_blocks_mutations_only(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        since = client.post("/api/pause", headers=AUTH).json()["stopped_at"]

        # Every gated mutating verb → 423 {error:stopped, since}. The gate fires BEFORE
        # any body/lookup, so even a would-be-404 (missing rule/action/pass) is 423.
        gated = [
            ("post", "/api/focus", {"instance": "main", "tabId": 1}),
            ("post", "/api/quick_links/ops", []),
            ("post", "/api/passes/pass-x/undo", {}),
            ("post", "/api/rules", {"pattern": "a.com", "instance_id": "main"}),
            ("put", "/api/rules/1", {"pattern": "a.com", "instance_id": "main"}),
            ("delete", "/api/rules/1", None),
            ("post", "/api/rules/1/reset", {}),
            ("post", "/api/actions/1/restore", {}),
            ("post", "/api/exemptions", {"instance_id": "main", "url": "https://a",
                                         "minutes": 30}),
            # DELETE carries the pair in the query string (httpx's `delete` shorthand
            # takes no body — which is exactly why the endpoint accepts both).
            ("delete", "/api/exemptions?instance_id=main&url=https%3A%2F%2Fa", None),
            ("post", "/api/instances/main/merge_windows", {}),
        ]
        for method, path, body in gated:
            fn = getattr(client, method)
            resp = fn(path, headers=AUTH) if body is None else fn(path, headers=AUTH, json=body)
            assert resp.status_code == 423, f"{method} {path} not gated"
            assert resp.json() == {"error": "stopped", "since": since}

        # Reads are NOT gated (the startpage must show the truth while stopped, §7).
        assert client.get("/api/state", headers=AUTH).status_code == 200
        assert client.get("/api/rules", headers=AUTH).status_code == 200
        assert client.get("/api/actions", headers=AUTH).status_code == 200
        # /api/state surfaces the stop so the "stopped since" row can render.
        assert client.get("/api/state", headers=AUTH).json()["stopped_at"] == since

        # rules/preview is read-only → NOT gated (may 200/422, never 423).
        pv = client.post(
            "/api/rules/preview", headers=AUTH,
            json={"op": "create", "pattern": "a.com", "instance_id": "main"},
        )
        assert pv.status_code != 423

        # run_pass{dry_run} is NOT gated (looking at the plan is why one stops, §7).
        dr = client.post("/api/run_pass", headers=AUTH, json={"dry_run": True})
        assert dr.status_code == 200 and dr.json()["status"] == "dry_run"

        # A real run_pass IS blocked, with the runner's own status shape.
        rp = client.post("/api/run_pass", headers=AUTH)
        assert rp.status_code == 200
        assert rp.json() == {"status": "stopped", "since": since}

        # Re-pressing the stop is NOT gated (POST /api/pause is a stop/start verb).
        assert client.post("/api/pause", headers=AUTH).status_code == 200


# --- run_pass{confirm_pending} refuses under an ACTIVE stop ------------------
def test_confirm_pending_refuses_under_active_stop(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        since = client.post("/api/pause", headers=AUTH).json()["stopped_at"]
        r = client.post("/api/run_pass", headers=AUTH, json={"confirm_pending": True})
        assert r.status_code == 200
        assert r.json() == {"status": "stopped", "since": since}


# --- DELETE /api/pause: start clears the stop and runs a pass ----------------
def test_delete_pause_starts_and_runs_pass(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        client.post("/api/pause", headers=AUTH)
        assert client.post("/api/focus", headers=AUTH, json={"instance": "main", "tabId": 1}).status_code == 423

        r = client.delete("/api/pause", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["resumed"] is True
        # A real pass was triggered immediately (no instances → no_ready_instances).
        assert body["pass"]["status"] == "no_ready_instances"

        # Stop fully cleared: the flag row is blank and the gate no longer fires.
        assert _setting(db_path, "curator_stopped_at") == ""
        assert client.get("/api/state", headers=AUTH).json()["stopped_at"] is None
        # A previously-gated verb now passes the gate (422: missing focus target ≠ 423).
        assert client.post(
            "/api/focus", headers=AUTH, json={"instance": "main", "tabId": 1}
        ).status_code in (200, 502, 422)


def test_delete_pause_shift_equals_actual_stop_duration(tmp_path):
    """The TTL shift on start is the REAL stopped time — however long the stop lasted
    (there is no requested duration to confuse it with). A protection issued at stop
    time moves by exactly that much, once."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # A stop pressed 30 minutes ago (seeded directly: the test controls the clock).
        now = client.get("/api/state", headers=AUTH).json()["server_now"]
        stopped_at = now - 30 * 60_000
        _set_setting(db_path, "curator_stopped_at", str(stopped_at))
        # A protection issued during the stop window (it outlives the stop START, so
        # the shift must move it forward by the stopped time).
        exemption_until = stopped_at + 5 * 60_000
        _w(db_path, "INSERT INTO exemptions (instance_id, url, until, reason) "
                    "VALUES ('main', 'https://x/y', ?, 'test')", (exemption_until,))

        body = client.delete("/api/pause", headers=AUTH).json()
        assert body["pass"]["status"] == "no_ready_instances", body["pass"]
        assert _passes(db_path) == 1
        assert _setting(db_path, "curator_stopped_at") == ""

        # The shift is the REAL stopped time (~30 min) and the protection moved by
        # exactly that much — once, not twice.
        shift = body["ttl_shift_ms"]
        assert 30 * 60_000 <= shift <= 31 * 60_000
        assert _q(db_path, "SELECT until FROM exemptions") == [(exemption_until + shift,)]

        # A second start is a guarded no-op: nothing left to shift.
        body2 = client.delete("/api/pause", headers=AUTH).json()
        assert body2["ttl_shift_ms"] == 0
        assert _q(db_path, "SELECT until FROM exemptions") == [(exemption_until + shift,)]


def test_delete_pause_without_a_stop_runs_an_ordinary_pass(tmp_path):
    """With nothing stopped and nothing latched the start degrades harmlessly: zero
    shift, one ordinary pass. (The runner turns the no-stop confirm_pending into an
    ordinary pass when no latch is armed — the over-threshold latch semantics have
    their own coverage in tests/test_curator_runner.py.)"""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        body = client.delete("/api/pause", headers=AUTH).json()
        assert body["pass"]["status"] == "no_ready_instances", body["pass"]
        assert body["ttl_shift_ms"] == 0               # there was no stop to shift
        assert _passes(db_path) == 1
        assert not _setting(db_path, "resume_pending")


# --- Старт vs latch-confirm: the TWO meanings of DELETE /api/pause (§7) ------
HOUR_MS = 3_600_000


def _tab(tab_id, url, *, age_ms=2 * HOUR_MS):
    """A snapshot TabInfo (§6 shape), idle long enough to be ruled and relocated."""
    return {
        "tabId": tab_id, "windowId": 1, "url": url, "title": "t", "favIconUrl": None,
        "pinned": False, "active": False, "audible": False,
        "ageMs": age_ms, "openedAgoMs": age_ms, "ageUnknown": False,
        "selfNavigating": False,
    }


def _answer_snapshot(ws, req, session, tabs):
    ws.send_json({
        "type": "snapshot", "id": req["id"], "sessionId": session,
        "focusedWindowId": None, "tabs": tabs,
        "windows": [{"id": 1, "type": "normal", "state": "normal"}],
    })


def _connect_instance(client, db_path, instance_id, session, tabs):
    """Enroll + hello + answer the initial snapshot_request; returns the live ws."""
    approve_instance(db_path, instance_id)
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json({
        "type": "hello", "protocolVersion": 1, "secret": secret_for(instance_id),
        "instanceId": instance_id, "installUuid": "u-" + instance_id,
        "origin": "chrome-extension://a", "title": instance_id,
        "sessionId": session, "allowExecuteJs": False,
    })
    _recv(ws)                                   # hello_ack
    _answer_snapshot(ws, _recv(ws), session, tabs)  # initial snapshot_request
    return ws


def test_start_after_a_stop_latches_an_over_threshold_plan_then_confirms(tmp_path):
    """«Старт» is NOT a confirm: a DELETE that lifts an actual stop runs the NORMAL
    threshold gate. The stopped UI renders the stop row, not the latched plan, so an
    over-threshold plan recomputed at start time must LATCH and be returned — never
    silently executed by the button that only meant "run again".

    The SECOND click — a DELETE with no stop set — is the informed confirm: by then
    the plan is on screen next to the button, so it executes and clears the latch.
    Reddens if ``resume_now`` goes back to passing ``confirm_pending``
    unconditionally (the first DELETE would execute both relocations)."""
    from concurrent.futures import ThreadPoolExecutor

    app = create_app(_settings(tmp_path, max_actions_per_pass=1,
                               snapshot_timeout_ms=2000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # Two ruled idle tabs on i1, homed to the (also connected) main: countable
        # phase-A = 2 > threshold 1.
        tabs = [_tab(10, "https://grafana.lc/d/a"), _tab(11, "https://grafana.lc/d/b")]
        ws_src = _connect_instance(client, db_path, "i1", "s-i1", tabs)
        ws_main = _connect_instance(client, db_path, "main", "s-main", [])
        pool = ThreadPoolExecutor(1)
        try:
            _w(db_path, "INSERT INTO rules (pattern, instance_id, singleton, invalid, "
                        "created_at) VALUES ('grafana.lc', 'main', 0, 0, 0)")
            client.post("/api/pause", headers=AUTH)

            # (a) DELETE while STOPPED: the start's pass runs the normal gate.
            fut = pool.submit(lambda: client.delete("/api/pause", headers=AUTH))
            _answer_snapshot(ws_src, _recv(ws_src), "s-i1", tabs)
            _answer_snapshot(ws_main, _recv(ws_main), "s-main", [])
            body = fut.result(timeout=10).json()

            assert body["resumed"] is True
            assert body["pass"]["status"] == "resume_pending", body["pass"]
            assert body["pass"]["plan"]["total"] == 2
            assert body["pass"]["plan"]["threshold"] == 1
            assert _setting(db_path, "curator_stopped_at") == ""   # started...
            assert _setting(db_path, "resume_pending")             # ...but latched
            # Phase A did NOT run: no relocate row hit the journal.
            assert _q(db_path, "SELECT COUNT(*) FROM actions WHERE kind='relocate'") \
                == [(0,)]

            # (b) the SECOND DELETE — no stop set now — is the informed confirm.
            fut2 = pool.submit(lambda: client.delete("/api/pause", headers=AUTH))
            _answer_snapshot(ws_src, _recv(ws_src), "s-i1", tabs)
            _answer_snapshot(ws_main, _recv(ws_main), "s-main", [])
            # The confirmed plan executes: phase A opens both copies in main.
            for n in range(2):
                cmd = _recv(ws_main)
                assert cmd["command"] == "open_tab"
                ws_main.send_json({"type": "response", "id": cmd["id"], "ok": True,
                                   "result": {"tabId": 900 + n, "windowId": 1}})
            body2 = fut2.result(timeout=10).json()

            assert body2["pass"]["status"] == "ok", body2["pass"]
            assert _q(db_path, "SELECT COUNT(*) FROM actions WHERE kind='relocate' "
                               "AND status='done'") == [(2,)]
            assert not _setting(db_path, "resume_pending")  # cleared by the execute
        finally:
            pool.shutdown(wait=False)
            ws_src.__exit__(None, None, None)
            ws_main.__exit__(None, None, None)


# --- §12 alert tie-in: overdue suppressed WHILE stopped ----------------------
def test_overdue_suppressed_while_stopped():
    interval_s = 300  # 5 min
    t0 = 1_000_000_000
    now = t0 + 12 * interval_s * 1000  # 12 intervals since the last finished pass
    snap = Snapshot(finished_at=t0)

    # STOPPED: overdue is 0 — a deliberate stop is silent however long it lasts (§7).
    snap.stopped_at = t0
    stopped = snap.stopped_at is not None
    assert _pass_overdue_seconds(snap, now, interval_s, stopped) == 0

    # Running: the SAME data reads hugely overdue — the zero above is the suppression
    # working, not a trivially quiet snapshot.
    snap.stopped_at = None
    assert _pass_overdue_seconds(snap, now, interval_s, False) >= 3 * interval_s


# --- §7's ONE exception: force:true for the human's own buttons -------------
def _seed_instance_and_action(db_path):
    import sqlite3
    from src.db.actions import insert_action
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        # Active row WITH a secret_hash so 'main' can authenticate as an INSTANCE caller
        # (issue #35 §4) — the force-verb tests below cross the stop only for that kind.
        conn.execute(
            "INSERT INTO instances (id, status, secret_hash, connected, session_id, "
            "snapshot_at) VALUES ('main', 'active', ?, 0, 's', 0)",
            (secret_hash_for("main"),),
        )
        aid = insert_action(
            conn, ts=1_000_000, kind="dedupe_close", status="done", initiator="curator",
            instance_from="main", url="https://x/y", url_norm="https://x/y",
            session_id_from="s",
        )
        conn.commit()
        return aid
    finally:
        conn.close()


def test_force_crosses_the_stop_gate_only_for_the_human_verbs(tmp_path):
    """§7: «Пауза глушит всю автоматику… Исключение — собственные кнопки человека, и
    то с явным `force:true`».

    The verbs §7 names — focus, restore, undo, merge_windows — accept the flag;
    everything else keeps answering 423 no matter what the body says. Reddens in BOTH
    directions: drop the ``force`` parameter and the first group 423s; wire it into the
    rest and the second group stops 423-ing."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        aid = _seed_instance_and_action(db_path)
        client.post("/api/pause", headers=AUTH)

        # The human at the startpage authenticates with the INSTANCE secret (§35 §4), and
        # force is honoured ONLY for that caller — so the forced buttons run as 'main'.
        instance_auth = instance_headers(secret_for("main"))

        # Human buttons WITH force: past the gate. They then fail on their own merits
        # (no live socket => 409/502/422), which is the point — the STOP no longer
        # decides, so anything but 423 proves the gate was crossed.
        forced = [
            ("/api/focus", {"instance": "main", "tabId": 1, "force": True}),
            (f"/api/actions/{aid}/restore", {"force": True}),
            ("/api/passes/pass-x/undo", {"force": True}),
            # §9's «Кнопка "слить окна сейчас" на стартпейдже» is a human button too.
            ("/api/instances/main/merge_windows", {"force": True}),
        ]
        for path, body in forced:
            resp = client.post(path, headers=instance_auth, json=body)
            assert resp.status_code != 423, f"{path} should honour force:true"

        # …and WITHOUT force they are still gated (force is explicit, never implied) —
        # same instance caller, so only the flag differs.
        for path, body in forced:
            plain = {k: v for k, v in body.items() if k != "force"}
            resp = client.post(path, headers=instance_auth, json=plain)
            assert resp.status_code == 423, f"{path} must stay gated without force"

        # NOT human buttons: force is ignored — a policy edit or an agent-shaped verb
        # under an emergency stop is exactly what the stop is for (§7).
        not_forcible = [
            ("post", "/api/rules", {"pattern": "a.com", "instance_id": "main",
                                    "force": True}),
            ("put", "/api/rules/1", {"pattern": "a.com", "instance_id": "main",
                                     "force": True}),
            ("post", "/api/rules/1/reset", {"force": True}),
            ("post", "/api/quick_links/ops", []),
            ("post", "/api/exemptions", {"instance_id": "main", "url": "https://a",
                                         "minutes": 5, "force": True}),
        ]
        for method, path, body in not_forcible:
            resp = getattr(client, method)(path, headers=AUTH, json=body)
            assert resp.status_code == 423, f"{method} {path} must ignore force"


def test_forced_restore_is_journaled_as_user_and_marked_in_detail(tmp_path):
    """§7: an action done with ``force`` is written as ``initiator=user`` and must be
    tellable apart in the archive afterwards — using the existing ``detail`` column, no
    new one. Reddens if the marker is dropped or the initiator changes."""
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    from src.db.actions import insert_action

    app = create_app(_settings(tmp_path, state_fresh_ms=3_000_000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")  # secret-hello needs an approved active row (#35)
        ws = client.websocket_connect("/ext").__enter__()
        try:
            ws.send_json({
                "type": "hello", "protocolVersion": 1,
                "secret": secret_for("i1"),
                "instanceId": "i1", "installUuid": "u", "origin": "chrome-extension://a",
                "title": "T", "sessionId": "sess-1", "allowExecuteJs": False,
            })
            _recv(ws)                       # hello_ack
            req = _recv(ws)                 # initial snapshot_request
            ws.send_json({"type": "snapshot", "id": req["id"], "sessionId": "sess-1",
                          "focusedWindowId": 1, "tabs": [],
                          "windows": [{"id": 1, "type": "normal", "state": "normal"}]})
            for _ in range(500):
                row = _q(db_path, "SELECT snapshot_at FROM instances WHERE id='i1'")
                if row and row[0][0] is not None:
                    break
                import time as _t
                _t.sleep(0.01)

            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout = 5000")
            aid = insert_action(
                conn, ts=1_000_000, kind="dedupe_close", status="done",
                initiator="curator", instance_from="i1", url="https://x/y",
                url_norm="https://x/y", session_id_from="sess-1",
            )
            conn.commit()
            conn.close()

            client.post("/api/pause", headers=AUTH)
            pool = ThreadPoolExecutor(1)
            # Authenticate as instance i1 (its RAW secret): force crosses the stop and
            # the row is written initiator='user' (the human), not 'admin' (§35 §4/§5).
            fut = pool.submit(lambda: client.post(
                f"/api/actions/{aid}/restore",
                headers=instance_headers(secret_for("i1")),
                json={"force": True},
            ))
            cmd = _recv(ws)
            assert cmd["command"] == "open_tab"     # the stop did NOT halt it
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 9, "windowId": 1}})
            assert fut.result(timeout=5).status_code == 200

            assert _q(
                db_path, "SELECT initiator, detail FROM actions WHERE kind='restore'"
            ) == [("user", "restore:force")]
        finally:
            ws.__exit__(None, None, None)
