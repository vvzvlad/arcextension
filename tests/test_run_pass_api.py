"""POST /api/run_pass (§7): auth, degraded, dry_run plan, empty real pass."""

from types import SimpleNamespace

from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
ADMIN_TOKEN = "test-admin-token"
# /api/* accepts either an admin (ADMIN_TOKEN) or an active-instance secret (issue #35 §4).
# The generic tests here just need a valid caller, so they use the admin credential;
# the force/pause tests that must EXECUTE a forced verb switch to an instance secret.
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0", port=8000,
        heartbeat_ms=600_000, protocol_version=1,
        ext_token=EXT_TOKEN, metrics_token="m", admin_token=ADMIN_TOKEN,
        ext_allowed_origins="",
        idle_minutes=60, pass_interval_min=5, tick_ms=60000,
        cmd_timeout_ms=1000, snapshot_timeout_ms=500, lease_ttl_ms=600_000,
        restore_exemption_min=120, pause_default_min=60, incomplete_after_min=15,
        self_nav_limit=10, state_fresh_ms=3000, quarantine_ttl_min=1440,
        actions_retention_days=90, js_audit_retention_days=730,
        main_instance_id="main", log_level="INFO",
        enroll_request_ttl_min=60,
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


def test_run_pass_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/api/run_pass").status_code == 401
        assert client.post("/api/run_pass", headers={"Authorization": "Bearer nope"}).status_code == 401
        client.app.state.degraded = True
        assert client.post("/api/run_pass", headers=AUTH).status_code == 503


def test_dry_run_returns_plan_without_writing(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        resp = client.post("/api/run_pass", headers=AUTH, json={"dry_run": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "dry_run"
        assert "plan" in body and body["plan"]["relocations"] == 0
        # dry_run writes NOTHING (§12): no actions, no passes row.
        assert _q(db_path, "SELECT COUNT(*) FROM actions") == [(0,)]
        assert _q(db_path, "SELECT COUNT(*) FROM passes") == [(0,)]


def test_real_pass_with_no_ready_instances_is_not_ok(tmp_path):
    # §12: a pass that found nobody ready is NOT healthy (ok=0), but still records a
    # passes row so "ran empty" is distinguishable from "no passes".
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        resp = client.post("/api/run_pass", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_ready_instances"
        rows = _q(db_path, "SELECT ok, instances_ready FROM passes")
        assert rows == [(0, 0)]
