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

from starlette.testclient import TestClient

from src.api.metrics import Snapshot, _pass_overdue_seconds
from src.app import create_app

EXT_TOKEN = "test-ext-token"
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0", port=8000,
        heartbeat_ms=600_000, protocol_version=1,
        ext_token=EXT_TOKEN, metrics_token="m", ext_allowed_origins="",
        idle_minutes=60, pass_interval_min=5, tick_ms=60000,
        cmd_timeout_ms=1000, snapshot_timeout_ms=200, lease_ttl_ms=600_000,
        restore_exemption_min=120, pause_default_min=60, incomplete_after_min=15,
        self_nav_limit=10, state_fresh_ms=3000, quarantine_ttl_min=1440,
        actions_retention_days=90, js_audit_retention_days=730,
        main_instance_id="main", log_level="INFO",
    )
    s.update(over)
    return SimpleNamespace(**s)


def _q(db_path, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


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


# --- §12 alert tie-in: overdue suppressed WHILE paused, grows after expiry ---
def test_overdue_suppressed_while_paused_grows_after_expiry():
    interval_s = 300  # 5 min
    t0 = 1_000_000_000
    now = t0 + 4 * interval_s * 1000  # 4 intervals since the last finished pass
    snap = Snapshot(finished_at=t0)

    # ACTIVE pause (pause_until > now): overdue is 0 — the routine hour is silent (§7).
    snap.pause_until = now + 3_600_000
    paused = snap.pause_until is not None and snap.pause_until > now
    assert _pass_overdue_seconds(snap, now, interval_s, paused) == 0

    # EXPIRED pause (waiting for the click): NOT paused → overdue grows past the
    # 3×PASS_INTERVAL alert threshold. Suppression lifts with the pause, not the click.
    snap.pause_until = t0 + interval_s  # in the past relative to `now`
    paused = snap.pause_until is not None and snap.pause_until > now
    assert paused is False
    assert _pass_overdue_seconds(snap, now, interval_s, paused) >= 3 * interval_s
