"""Фаза 16 — pause WRITE side HTTP tests (§7 «Пауза»).

Drives the real endpoints through a TestClient:

* ``POST /api/pause`` arms/extends a FINITE pause and returns the deadline + start,
* the all-mutation gate answers ``423 {error:"paused", until}`` on every mutating
  ``/api/*`` verb while paused — but NOT on reads, ``rules/preview``, the resume verbs
  or ``run_pass{dry_run}``,
* ``DELETE /api/pause`` resumes (clears the pause) and triggers a pass immediately,
* ``run_pass{confirm_pending}`` still refuses under a live pause (SUGGESTION 7),

plus the §12 alert tie-in as a pure-function check: overdue is suppressed WHILE paused
and grows once the pause has expired (the click-wait fires the alert).
"""

from types import SimpleNamespace

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
# the force/pause tests that must EXECUTE a forced verb switch to an instance secret.
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
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


def _seed_fleet(db_path, instance_id="main"):
    """One instances row — enough to make ``fleet_populated`` true.

    It is NEVER connected, so a pass over it is a real but empty one
    (``no_ready_instances``). The continuity policy keys on the fleet EXISTING, not on
    it answering: a pass that saw no fleet deliberately stores no fingerprint (§7).
    """
    _w(db_path, "INSERT OR IGNORE INTO instances (id, status, connected, conn_epoch) "
                "VALUES (?, 'active', 0, 0)", (instance_id,))


def _store_fingerprint(db_path, *, idle_minutes):
    """Record a continuity fingerprint taken at ``idle_minutes``.

    Called with the app's own IDLE_MINUTES it ESTABLISHES continuity; called with any
    other value it manufactures a §7 continuity break (a lowered IDLE_MINUTES is one of
    the breaks the fingerprint exists to catch) — with no pause anywhere in sight, which
    is the case the resume button had no answer for.
    """
    import sqlite3

    from src.curator import clock as clockmod

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        fp = clockmod.current_fingerprint(
            conn, idle_minutes=idle_minutes, main_instance_id="main"
        )
        clockmod.store_fingerprint(conn, fp)
        conn.commit()
    finally:
        conn.close()


def _stored_fingerprint(db_path):
    import json
    return json.loads(_setting(db_path, "curator_continuity_fingerprint"))


def _passes(db_path):
    return _q(db_path, "SELECT COUNT(*) FROM passes")[0][0]


# --- POST /api/pause: arms, finite, extend keeps the start ------------------
def test_post_pause_arms_finite_and_extend_keeps_start(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # Default minutes (no body) → PAUSE_DEFAULT_MIN.
        r1 = client.post("/api/pause", headers=AUTH)
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["paused_until"] > b1["pause_started_at"]
        assert b1["paused_until"] - b1["pause_started_at"] == 60 * 60_000

        # An absurd request is CLAMPED — a pause is always finite (§7).
        r2 = client.post("/api/pause", headers=AUTH, json={"minutes": 10**9})
        b2 = r2.json()
        assert b2["pause_started_at"] == b1["pause_started_at"]  # extend keeps start
        # Capped: the deadline is at most `cap` minutes past the (later) request time,
        # so the span from the ORIGINAL start is cap + the small elapsed — never the
        # absurd requested value. Bounded proves finiteness.
        span = b2["paused_until"] - b2["pause_started_at"]
        assert 1440 * 60_000 <= span <= 1440 * 60_000 + 60_000

        # A non-int minutes is a 422 (never a silent infinite pause).
        assert client.post("/api/pause", headers=AUTH, json={"minutes": "x"}).status_code == 422

        # The pause row is set to the clamped deadline.
        assert _q(db_path, "SELECT value FROM settings WHERE key='pause_until'") == [
            (str(b2["paused_until"]),)
        ]


# --- the all-mutation gate --------------------------------------------------
def test_pause_gate_blocks_mutations_only(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        arm = client.post("/api/pause", headers=AUTH, json={"minutes": 60}).json()
        until = arm["paused_until"]

        # Every gated mutating verb → 423 {error:paused, until}. The gate fires BEFORE
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
            assert resp.json() == {"error": "paused", "until": until}

        # Reads are NOT gated (the startpage must show the truth during a pause, §7).
        assert client.get("/api/state", headers=AUTH).status_code == 200
        assert client.get("/api/rules", headers=AUTH).status_code == 200
        assert client.get("/api/actions", headers=AUTH).status_code == 200
        # /api/state surfaces the pause so the countdown row can render.
        assert client.get("/api/state", headers=AUTH).json()["paused_until"] == until

        # rules/preview is read-only → NOT gated (may 200/422, never 423).
        pv = client.post(
            "/api/rules/preview", headers=AUTH,
            json={"op": "create", "pattern": "a.com", "instance_id": "main"},
        )
        assert pv.status_code != 423

        # run_pass{dry_run} is NOT gated (looking at the plan is why one pauses, §7).
        dr = client.post("/api/run_pass", headers=AUTH, json={"dry_run": True})
        assert dr.status_code == 200 and dr.json()["status"] == "dry_run"

        # Extending the pause is NOT gated (POST /api/pause is a resume-family verb).
        assert client.post("/api/pause", headers=AUTH, json={"minutes": 30}).status_code == 200


# --- run_pass{confirm_pending} refuses under an ACTIVE pause (SUGGESTION 7) --
def test_confirm_pending_refuses_under_active_pause(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        client.post("/api/pause", headers=AUTH, json={"minutes": 60})
        r = client.post("/api/run_pass", headers=AUTH, json={"confirm_pending": True})
        assert r.status_code == 200 and r.json()["status"] == "paused"


# --- DELETE /api/pause: resume clears the pause and runs a pass -------------
def test_delete_pause_resumes_and_runs_pass(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        client.post("/api/pause", headers=AUTH, json={"minutes": 60})
        assert client.post("/api/focus", headers=AUTH, json={"instance": "main", "tabId": 1}).status_code == 423

        r = client.delete("/api/pause", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["resumed"] is True
        # A real pass was triggered immediately (no instances → no_ready_instances).
        assert body["pass"]["status"] == "no_ready_instances"

        # Pause fully cleared: the deadline row is blank and the gate no longer fires.
        assert _q(db_path, "SELECT value FROM settings WHERE key='pause_until'") == [("",)]
        assert client.get("/api/state", headers=AUTH).json()["paused_until"] is None
        # A previously-gated verb now passes the gate (422: missing focus target ≠ 423).
        assert client.post(
            "/api/focus", headers=AUTH, json={"instance": "main", "tabId": 1}
        ).status_code in (200, 502, 422)


# --- the button is the ONLY exit from a continuity-break latch (§7) ---------
def test_delete_pause_escapes_a_continuity_break_latch(tmp_path):
    """«Запустить сейчас» must get the curator out of ``resume_pending``, whichever
    event armed it — and a CONTINUITY BREAK arms it just as an expired pause does.

    On a break there is no pause to clear: the only thing standing between the curator
    and normal operation is the stale fingerprint, which is refreshed by a REAL pass and
    by nothing else — and a real pass is exactly what the latch blocks (an unconfirmed
    pass returns ``{"status": "resume_pending"}`` at step 1 before doing anything). So
    every press cleared nothing and ran nothing, the latch stayed armed, and the state
    had no exit through the UI at all.

    Reddens on the whole chain: the pass status, the ``passes`` row, the latch and the
    refreshed fingerprint. Drop the confirm from ``resume_now`` (or clear the latch
    before the pass, which makes the runner degrade the confirm to an ordinary pass) and
    the response is another ``resume_pending`` with nothing done.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_fleet(db_path)
        _store_fingerprint(db_path, idle_minutes=30)   # the service runs at 60 → break

        # The scheduled pass defers behind a click: plan shown, nothing executed.
        armed = client.post("/api/run_pass", headers=AUTH).json()
        assert armed["status"] == "resume_pending"
        assert _passes(db_path) == 0
        state = client.get("/api/state", headers=AUTH).json()
        assert state["resume_pending"] is True and state["paused_until"] is None

        # The button.
        body = client.delete("/api/pause", headers=AUTH).json()
        assert body["resumed"] is True
        # A REAL pass ran — not another deferral — and left its row in `passes`.
        assert body["pass"]["status"] == "no_ready_instances", body["pass"]
        assert _passes(db_path) == 1

        # The latch is gone AND the fingerprint was refreshed to the running config.
        # Both halves matter: without the refresh the very next tick re-arms on the same
        # stale fingerprint and the button-press achieved nothing durable.
        assert not _setting(db_path, "resume_pending")
        assert _stored_fingerprint(db_path)["idle_minutes"] == 60
        assert client.get("/api/state", headers=AUTH).json()["resume_pending"] is False

        # …and the next pass is an ordinary one, not a re-armed latch.
        assert client.post("/api/run_pass", headers=AUTH).json()["status"] == "no_ready_instances"
        assert _passes(db_path) == 2


def test_delete_pause_escapes_a_pause_expiry_latch_and_shifts_ttls_once(tmp_path):
    """The branch that ALREADY worked must keep working — the latch armed by a pause
    that ended by TIMEOUT (§7: only a manual resume runs at once; an expiry defers
    behind a click).

    Also pins the TTL shift at exactly ONE application. The confirming pass runs its own
    ``apply_resume_shift`` (§7 shifts the protections before any decision), so a
    protection issued before the pause must move by the real pause duration and NOT
    twice — the once-guard is ``pause_started_at``, which the resume already cleared.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_fleet(db_path)
        _store_fingerprint(db_path, idle_minutes=60)   # continuity is FINE here

        # A pause that expired a minute ago and was never lifted by hand: the deadline
        # is in the past while the start is still armed.
        now = client.get("/api/state", headers=AUTH).json()["server_now"]
        started, until = now - 30 * 60_000, now - 60_000
        _set_setting(db_path, "pause_started_at", str(started))
        _set_setting(db_path, "pause_until", str(until))
        # A protection issued during the pause window (it outlives the pause START, so
        # the shift must move it forward by the paused time).
        exemption_until = started + 5 * 60_000
        _w(db_path, "INSERT INTO exemptions (instance_id, url, until, reason) "
                    "VALUES ('main', 'https://x/y', ?, 'test')", (exemption_until,))

        # The expiry defers behind a click: a dry-run plan, no `passes` row.
        assert client.post("/api/run_pass", headers=AUTH).json()["status"] == "resume_pending"
        assert _passes(db_path) == 0

        body = client.delete("/api/pause", headers=AUTH).json()
        assert body["pass"]["status"] == "no_ready_instances", body["pass"]
        assert _passes(db_path) == 1
        assert not _setting(db_path, "resume_pending")
        assert _setting(db_path, "pause_until") == ""
        assert _setting(db_path, "pause_started_at") == ""

        # The shift is the REAL elapsed pause (~30 min, capped at the deadline) and the
        # protection moved by exactly that much — once, not twice.
        shift = body["ttl_shift_ms"]
        assert 29 * 60_000 <= shift <= 30 * 60_000
        assert _q(db_path, "SELECT until FROM exemptions") == [(exemption_until + shift,)]


def test_delete_pause_without_a_latch_runs_an_ordinary_pass(tmp_path):
    """With nothing armed the confirm degrades harmlessly — and the degradation is not
    a back door either.

    Three presses, three different meanings: an ordinary pass when nothing is pending; a
    plan (never an execution) for a break the human has not been shown yet; the
    execution once that plan is the armed one. The middle case is the guard against
    "fixing" the deadlock by honouring ``confirm_pending`` unconditionally, which would
    let the button eat a continuity break silently.
    """
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_fleet(db_path)
        _store_fingerprint(db_path, idle_minutes=60)   # continuity established, no pause

        first = client.delete("/api/pause", headers=AUTH).json()
        assert first["pass"]["status"] == "no_ready_instances", first["pass"]
        assert first["ttl_shift_ms"] == 0               # there was no pause to shift
        assert _passes(db_path) == 1
        assert not _setting(db_path, "resume_pending")

        # A break lands (IDLE_MINUTES 60 -> 30 in the stored fingerprint) and nobody has
        # seen its plan yet: the button ARMS the latch and shows the plan instead of
        # draining the fleet on a flag the client sends by construction.
        _store_fingerprint(db_path, idle_minutes=30)
        second = client.delete("/api/pause", headers=AUTH).json()
        assert second["pass"]["status"] == "resume_pending", second["pass"]
        assert _passes(db_path) == 1                    # no real pass ran
        assert _setting(db_path, "resume_pending")

        # Pressing again confirms THAT plan — so the button always terminates.
        third = client.delete("/api/pause", headers=AUTH).json()
        assert third["pass"]["status"] == "no_ready_instances", third["pass"]
        assert _passes(db_path) == 2
        assert not _setting(db_path, "resume_pending")


# --- §12 alert tie-in: overdue suppressed WHILE paused, grows after expiry ---
def test_overdue_suppressed_while_paused_grows_after_expiry():
    interval_s = 300  # 5 min
    t0 = 1_000_000_000
    now = t0 + 12 * interval_s * 1000  # 12 intervals since the last finished pass
    snap = Snapshot(finished_at=t0)

    # ACTIVE pause (pause_until > now): overdue is 0 — the routine hour is silent (§7).
    snap.pause_until = now + 3_600_000
    paused = snap.pause_until is not None and snap.pause_until > now
    assert _pass_overdue_seconds(snap, now, interval_s, paused) == 0

    # EXPIRED pause (waiting for the click): NOT paused → overdue grows past the
    # 3×PASS_INTERVAL alert threshold. Suppression lifts with the pause, not the click.
    # The pause lapsed 8 intervals ago, so the click-wait is genuinely overdue.
    snap.pause_until = t0 + 4 * interval_s * 1000
    paused = snap.pause_until is not None and snap.pause_until > now
    assert paused is False
    assert _pass_overdue_seconds(snap, now, interval_s, paused) >= 3 * interval_s


def test_overdue_after_expiry_is_anchored_to_the_expiry_moment():
    """§7: after a pause expires the clock restarts from the EXPIRY, not last_pass_ts.

    A three-hour pause taken right after a pass would otherwise make the gauge read
    ~3h overdue the very second it lapsed — the alert fires immediately, while §7
    promises 3×PASS_INTERVAL for the human to see the pending plan and click. Reddens
    to a big number if the ``max(reference, pause_until)`` anchor is removed.
    """
    interval_s = 300
    t0 = 1_000_000_000
    # Last pass finished at t0; a 3h pause ran from just after it and has just lapsed.
    pause_until = t0 + 3 * 3_600_000
    snap = Snapshot(finished_at=t0, pause_until=pause_until)

    # The second the pause lapses: nothing is overdue yet.
    assert _pass_overdue_seconds(snap, pause_until, interval_s, False) == 0
    # Still inside the grace window §7 promises (2 intervals after expiry).
    at_2_intervals = pause_until + 2 * interval_s * 1000
    assert _pass_overdue_seconds(snap, at_2_intervals, interval_s, False) < 3 * interval_s
    # Past it: the click-wait IS a degraded state and must alert (§7).
    at_5_intervals = pause_until + 5 * interval_s * 1000
    assert _pass_overdue_seconds(snap, at_5_intervals, interval_s, False) >= 3 * interval_s

    # Non-vacuity: with the SAME data and no pause row, the gauge is already huge —
    # so the zero above is the anchor working, not a trivially quiet snapshot.
    no_pause = Snapshot(finished_at=t0)
    assert _pass_overdue_seconds(no_pause, pause_until, interval_s, False) > 3 * 3600 - 600


def test_overdue_ignores_a_pause_that_ended_before_the_last_pass():
    # A pause resumed long ago must not hold the anchor: `max(reference, pause_until)`
    # keeps the LAST PASS as the reference when the pause is older than it.
    interval_s = 300
    t0 = 1_000_000_000
    snap = Snapshot(finished_at=t0, pause_until=t0 - 10 * 3_600_000)
    now = t0 + 10 * interval_s * 1000
    assert _pass_overdue_seconds(snap, now, interval_s, False) == 9 * interval_s


# --- §7's ONE exception: force:true for the human's own buttons -------------
def _seed_instance_and_action(db_path):
    import sqlite3
    from src.db.actions import insert_action
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        # Active row WITH a secret_hash so 'main' can authenticate as an INSTANCE caller
        # (issue #35 §4) — the force-verb tests below cross the pause only for that kind.
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


def test_force_crosses_the_pause_gate_only_for_the_human_verbs(tmp_path):
    """§7: «Пауза глушит всю автоматику… Исключение — собственные кнопки человека, и
    то с явным `force:true`».

    The three verbs §7 names — focus, restore, undo — accept the flag; everything else
    keeps answering 423 no matter what the body says. Reddens in BOTH directions: drop
    the ``force`` parameter and the first group 423s; wire it into the rest and the
    second group stops 423-ing."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        aid = _seed_instance_and_action(db_path)
        client.post("/api/pause", headers=AUTH, json={"minutes": 60})

        # The human at the startpage authenticates with the INSTANCE secret (§35 §4), and
        # force is honoured ONLY for that caller — so the forced buttons run as 'main'.
        instance_auth = instance_headers(secret_for("main"))

        # Human buttons WITH force: past the gate. They then fail on their own merits
        # (no live socket => 409/502/422), which is the point — the PAUSE no longer
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

            client.post("/api/pause", headers=AUTH, json={"minutes": 60})
            pool = ThreadPoolExecutor(1)
            # Authenticate as instance i1 (its RAW secret): force crosses the pause and
            # the row is written initiator='user' (the human), not 'admin' (§35 §4/§5).
            fut = pool.submit(lambda: client.post(
                f"/api/actions/{aid}/restore",
                headers=instance_headers(secret_for("i1")),
                json={"force": True},
            ))
            cmd = _recv(ws)
            assert cmd["command"] == "open_tab"     # the pause did NOT stop it
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True,
                          "result": {"tabId": 9, "windowId": 1}})
            assert fut.result(timeout=5).status_code == 200

            assert _q(
                db_path, "SELECT initiator, detail FROM actions WHERE kind='restore'"
            ) == [("user", "restore:force")]
        finally:
            ws.__exit__(None, None, None)
