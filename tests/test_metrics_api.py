"""GET /metrics — the Prometheus exposition (§12).

Covers: the separate METRICS_TOKEN guard (EXT_TOKEN rejected), every §12 metric
present from the first scrape, pass facts read from `passes` (incl. the green-and-dead
last_pass_ok=0), the two acceptance scenarios (hour-long pause suppresses overdue +
snapshot_age so NO alert can fire; a crash-loop trips overdue computed from `passes`
and survives a fresh process), the actions-last-pass zeroing, main-never-seen, the
auth-rejections counter, degraded-mode serving, and the alerts.yml convention.

Each assertion is written so that removing the guard it names reddens the test.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import yaml
from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
METRICS_TOKEN = "test-metrics-token"
MAUTH = {"Authorization": f"Bearer {METRICS_TOKEN}"}

# Every metric name §12 mandates. Presence of ALL of these on a FRESH scrape is the
# "start value present, never NoData in the failure it catches" contract.
ALL_METRICS = [
    "curator_last_pass_ts",
    "curator_last_pass_ok",
    "curator_actions_last_pass",
    "curator_instance_connected",
    "curator_instance_last_seen_ts",
    "curator_instance_absent_seconds",
    "curator_auth_rejections_total",
    "curator_deferred_total",
    "curator_quarantined_total",
    "curator_paused_until",
    "curator_resume_pending",
    "curator_rules_total",
    "curator_rules_invalid",
    "curator_relocations_incomplete",
    "curator_main_instance_never_seen",
    "curator_instance_snapshot_age_seconds",
    "curator_clock_step_seconds",
    "curator_pass_overdue_seconds",
    "curator_backup_age_seconds",
    "curator_backup_last_ok_ts",
    "curator_backup_bytes",
    "curator_migration_failed",
]


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0",
        port=8000,
        heartbeat_ms=600_000,
        protocol_version=1,
        ext_token=EXT_TOKEN,
        metrics_token=METRICS_TOKEN,
        ext_allowed_origins="",
        cmd_timeout_ms=2000,
        snapshot_timeout_ms=2000,
        state_fresh_ms=3_000_000,
        restore_exemption_min=120,
        actions_retention_days=90,
        js_audit_retention_days=730,
        lease_ttl_ms=600_000,
        pass_interval_min=5,          # 6×PASS_INTERVAL = 1800s
        idle_minutes=60,
        main_instance_id="main",
    )
    s.update(over)
    return SimpleNamespace(**s)


# --- direct-DB seeding (the mirror the endpoint reads) ----------------------
def _exec(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _insert_pass(db_path, pass_id, started_at, finished_at=None, ok=None,
                 instances_ready=None):
    _exec(
        db_path,
        "INSERT INTO passes (pass_id, started_at, finished_at, ok, instances_ready) "
        "VALUES (?,?,?,?,?)",
        (pass_id, started_at, finished_at, ok, instances_ready),
    )


def _insert_instance(db_path, iid, connected=0, last_seen_at=None, snapshot_at=None):
    _exec(
        db_path,
        "INSERT INTO instances (id, connected, last_seen_at, snapshot_at) VALUES (?,?,?,?)",
        (iid, connected, last_seen_at, snapshot_at),
    )


def _insert_action(db_path, pass_id, kind, status="done", instance_to=None, reason=None):
    _exec(
        db_path,
        "INSERT INTO actions (pass_id, ts, kind, status, initiator, instance_to, reason) "
        "VALUES (?,?,?,?,?,?,?)",
        (pass_id, 1, kind, status, "curator", instance_to, reason),
    )


def _set_setting(db_path, key, value):
    _exec(
        db_path,
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --- exposition parsing -----------------------------------------------------
def _samples(body, name):
    """All sample lines of ``name`` as ``(labels_str, value_float)`` (skips HELP/TYPE)."""
    out = []
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(name + " "):
            out.append(("", float(line[len(name) + 1:].strip())))
        elif line.startswith(name + "{"):
            labels, _, val = line[len(name) + 1:].partition("} ")
            out.append((labels, float(val.strip())))
    return out


def _scalar(body, name):
    s = _samples(body, name)
    assert len(s) == 1 and s[0][0] == "", f"{name}: expected one unlabelled sample, got {s}"
    return s[0][1]


def _by_label(body, name, key, value):
    for labels, val in _samples(body, name):
        if f'{key}="{value}"' in labels:
            return val
    return None


def _scrape(client):
    r = client.get("/metrics", headers=MAUTH)
    assert r.status_code == 200
    return r.text


# --- auth -------------------------------------------------------------------
def test_metrics_requires_metrics_token_and_rejects_ext_token(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get(
            "/metrics", headers={"Authorization": "Bearer nope"}
        ).status_code == 401
        # EXT_TOKEN must NOT open /metrics (§12: the scrape credential is separate).
        assert client.get(
            "/metrics", headers={"Authorization": f"Bearer {EXT_TOKEN}"}
        ).status_code == 401
        r = client.get("/metrics", headers=MAUTH)
        assert r.status_code == 200
        ct = r.headers["content-type"]
        assert "text/plain" in ct and "version=0.0.4" in ct


# --- all metrics present at start -------------------------------------------
def test_all_metrics_present_on_fresh_scrape(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        body = _scrape(client)
        for name in ALL_METRICS:
            assert f"# HELP {name} " in body, f"missing HELP for {name}"
            assert f"# TYPE {name} " in body, f"missing TYPE for {name}"
        # Scalars carry a value even with an empty DB (never NoData in the outage).
        assert _scalar(body, "curator_last_pass_ok") == 0.0
        assert _scalar(body, "curator_rules_total") == 0.0
        assert _scalar(body, "curator_main_instance_never_seen") == 1.0
        assert _scalar(body, "curator_migration_failed") == 0.0


# --- pass facts from `passes` (incl. green-and-dead) ------------------------
def test_pass_facts_read_from_passes(tmp_path):
    s = _settings(tmp_path)
    app = create_app(s)
    now = int(time.time() * 1000)
    with TestClient(app) as client:
        _insert_pass(s.db_path, "p-old", now - 600_000, now - 590_000, ok=1, instances_ready=1)
        _insert_pass(s.db_path, "p-new", now - 60_000, now - 50_000, ok=1, instances_ready=2)
        _insert_action(s.db_path, "p-new", "dedupe_close")
        _insert_action(s.db_path, "p-new", "dedupe_close")
        body = _scrape(client)
        # last_pass_ts = newest finished_at; last_pass_ok = 1 (ok + ready>0).
        assert _scalar(body, "curator_last_pass_ts") == float(now - 50_000)
        assert _scalar(body, "curator_last_pass_ok") == 1.0
        assert _by_label(body, "curator_actions_last_pass", "kind", "dedupe_close") == 2.0


def test_last_pass_ok_zero_when_no_ready_instances(tmp_path):
    # «Зелено и мертво»: the pass finished ok=1 but found zero ready instances -> 0.
    s = _settings(tmp_path)
    app = create_app(s)
    now = int(time.time() * 1000)
    with TestClient(app) as client:
        _insert_pass(s.db_path, "p1", now - 60_000, now - 50_000, ok=1, instances_ready=0)
        body = _scrape(client)
        assert _scalar(body, "curator_last_pass_ok") == 0.0


# --- ACCEPTANCE #1: an hour-long pause fires no alert ------------------------
def test_pause_suppresses_overdue_and_snapshot_age(tmp_path):
    s = _settings(tmp_path)
    app = create_app(s)
    now = int(time.time() * 1000)
    with TestClient(app) as client:
        # A situation that WOULD alert: last pass finished 2h ago, a connected
        # instance whose snapshot is 2h stale.
        _insert_pass(s.db_path, "p1", now - 7_200_000, now - 7_200_000, ok=1, instances_ready=1)
        _insert_instance(s.db_path, "main", connected=1, last_seen_at=now,
                         snapshot_at=now - 7_200_000)
        _set_setting(s.db_path, "pause_until", now + 3_600_000)  # +60min

        body = _scrape(client)
        # Suppression lives in the GAUGE: both read 0 while paused, so NO rule reading
        # them can fire (acceptance #1). Remove the pause-branch in the gauge and these
        # become ~6900 / ~7200 and redden.
        assert _scalar(body, "curator_pass_overdue_seconds") == 0.0
        assert _by_label(body, "curator_instance_snapshot_age_seconds", "id", "main") == 0.0

        # Non-vacuity: clear the pause and the SAME data now reads > 0 (the gauge was
        # genuinely suppressed, not trivially zero).
        _set_setting(s.db_path, "pause_until", "")
        body2 = _scrape(client)
        assert _scalar(body2, "curator_pass_overdue_seconds") > 1800
        assert _by_label(body2, "curator_instance_snapshot_age_seconds", "id", "main") > 1800


# --- ACCEPTANCE #2: a crash-loop trips "no pass happened" -------------------
def test_crash_loop_overdue_from_passes_survives_restart(tmp_path):
    s = _settings(tmp_path)
    now = int(time.time() * 1000)
    # A crash loop: passes keep STARTING but none ever finishes (finished_at NULL).
    # The oldest started_at is the durable anchor -> overdue grows and is NOT reset by
    # a fresh process, because it is computed from `passes`, not process memory.
    app = create_app(s)
    with TestClient(app) as client:
        _insert_pass(s.db_path, "c1", now - 3_600_000)  # oldest start, never finished
        _insert_pass(s.db_path, "c2", now - 60_000)     # newest start, never finished
        body = _scrape(client)
        assert _scalar(body, "curator_last_pass_ok") == 0.0
        assert _scalar(body, "curator_pass_overdue_seconds") > 1800

    # "Restart": a brand-new app object (empty process memory) on the SAME DB still
    # reports overdue — proving the gauge is DB-derived, not a memory value reset to 0.
    app2 = create_app(_settings(tmp_path))
    with TestClient(app2) as client2:
        body2 = _scrape(client2)
        assert _scalar(body2, "curator_pass_overdue_seconds") > 1800


def test_overdue_from_old_finished_pass(tmp_path):
    # The other crash-loop shape §12 names: an old FINISHED pass and nothing since.
    s = _settings(tmp_path)
    app = create_app(s)
    now = int(time.time() * 1000)
    with TestClient(app) as client:
        _insert_pass(s.db_path, "f1", now - 3_600_000, now - 3_600_000, ok=1, instances_ready=1)
        body = _scrape(client)
        assert _scalar(body, "curator_pass_overdue_seconds") > 1800


# --- actions_last_pass zeroing across passes --------------------------------
def test_actions_last_pass_zeroes_previous_kinds(tmp_path):
    s = _settings(tmp_path)
    app = create_app(s)
    now = int(time.time() * 1000)
    with TestClient(app) as client:
        _insert_pass(s.db_path, "p1", now - 600_000, now - 590_000, ok=1, instances_ready=1)
        _insert_action(s.db_path, "p1", "dedupe_close")
        _insert_pass(s.db_path, "p2", now - 60_000, now - 50_000, ok=1, instances_ready=1)
        _insert_action(s.db_path, "p2", "relocate_close")
        body = _scrape(client)
        # Only the newest pass's kind is present; the previous pass's kind is gone
        # (scoped to the last pass_id => zeroed for free). Remove that scoping and
        # dedupe_close lingers, reddening this.
        assert _by_label(body, "curator_actions_last_pass", "kind", "relocate_close") == 1.0
        assert _by_label(body, "curator_actions_last_pass", "kind", "dedupe_close") is None


# --- main-never-seen (sticky stock-branch-off fact) -------------------------
def test_main_instance_never_seen(tmp_path):
    s = _settings(tmp_path)
    app = create_app(s)
    now = int(time.time() * 1000)
    with TestClient(app) as client:
        assert _scalar(_scrape(client), "curator_main_instance_never_seen") == 1.0
        # A row with last_seen_at NULL is still "never seen".
        _insert_instance(s.db_path, "main", connected=1, last_seen_at=None)
        assert _scalar(_scrape(client), "curator_main_instance_never_seen") == 1.0
        # Once actually seen -> 0.
        _exec(s.db_path, "UPDATE instances SET last_seen_at = ? WHERE id = 'main'", (now,))
        assert _scalar(_scrape(client), "curator_main_instance_never_seen") == 0.0


# --- deferred / quarantine / relocations ------------------------------------
def test_deferred_quarantine_relocations(tmp_path):
    s = _settings(tmp_path)
    app = create_app(s)
    now = int(time.time() * 1000)
    with TestClient(app) as client:
        _insert_pass(s.db_path, "p1", now - 60_000, now - 50_000, ok=1, instances_ready=1)
        _insert_action(s.db_path, "p1", "relocate", status="deferred",
                       instance_to="prox", reason="3")
        _exec(s.db_path,
              "INSERT INTO quarantine (instance_id, url, until) VALUES (?,?,?)",
              ("main", "https://x/y", now + 60_000))       # active
        _exec(s.db_path,
              "INSERT INTO quarantine (instance_id, url, until) VALUES (?,?,?)",
              ("main", "https://x/z", now - 60_000))        # expired -> not counted
        body = _scrape(client)
        assert _by_label(body, "curator_deferred_total", "to_instance", "prox") == 3.0
        assert _scalar(body, "curator_quarantined_total") == 1.0
        assert _scalar(body, "curator_relocations_incomplete") == 0.0


# --- backup dir reading -----------------------------------------------------
def test_backup_metrics_from_dir(tmp_path):
    s = _settings(tmp_path)
    app = create_app(s)
    with TestClient(app) as client:
        # No backup yet -> age is large (would alert), bytes/last_ok are 0.
        body0 = _scrape(client)
        assert _scalar(body0, "curator_backup_age_seconds") > 93600
        assert _scalar(body0, "curator_backup_bytes") == 0.0
        # A finished copy present -> bytes>0, small age, last_ok>0.
        bdir = Path(s.backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        copy = bdir / "curator-20990101T000000_000000.db"
        copy.write_bytes(b"x" * 128)
        body1 = _scrape(client)
        assert _scalar(body1, "curator_backup_bytes") == 128.0
        assert _scalar(body1, "curator_backup_age_seconds") < 93600
        assert _scalar(body1, "curator_backup_last_ok_ts") > 0.0


# --- clock step read from settings ------------------------------------------
def test_clock_step_seconds_from_settings(tmp_path):
    s = _settings(tmp_path)
    app = create_app(s)
    with TestClient(app) as client:
        assert _scalar(_scrape(client), "curator_clock_step_seconds") == 0.0
        _set_setting(s.db_path, "curator_clock_step_seconds", "42.5")
        assert _scalar(_scrape(client), "curator_clock_step_seconds") == 42.5


# --- auth-rejections counter increments -------------------------------------
def test_auth_rejections_counter_increments(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        before = _scalar(_scrape(client), "curator_auth_rejections_total")
        # One rejected request (wrong token) between two good scrapes.
        assert client.get(
            "/metrics", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        after = _scalar(_scrape(client), "curator_auth_rejections_total")
        assert after >= before + 1


# --- degraded mode still serves /metrics ------------------------------------
def test_metrics_served_in_degraded_mode(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        client.app.state.degraded = True
        body = _scrape(client)
        assert _scalar(body, "curator_migration_failed") == 1.0
        # Even with the DB pulled out entirely, /metrics answers (no 500) and flags it.
        client.app.state.db = None
        body2 = _scrape(client)
        assert _scalar(body2, "curator_migration_failed") == 1.0
        for name in ALL_METRICS:
            assert f"# HELP {name} " in body2


# --- alerts.yml convention --------------------------------------------------
def test_alerts_yml_convention():
    path = Path(__file__).resolve().parents[1] / "deploy" / "alerts.yml"
    doc = yaml.safe_load(path.read_text())
    rules = [r for g in doc["groups"] for r in g["rules"]]
    assert rules, "no rules parsed"

    alerting = [r for r in rules if r.get("noDataState") == "Alerting"]
    ok_rules = [r for r in rules if r.get("noDataState") == "OK"]
    # Exactly ONE Alerting-on-NoData rule (target loss); every other rule is OK.
    assert len(alerting) == 1
    assert alerting[0]["expr"].startswith('up{job="curator"}')
    assert len(ok_rules) == len(rules) - 1
    # Every rule declares a noDataState (no rule silently defaults).
    assert all(r.get("noDataState") in ("OK", "Alerting") for r in rules)
    # No expression uses the time()-metric filter-to-NoData form the spec forbids.
    for r in rules:
        assert "time(" not in r["expr"], f"forbidden time() filter in {r.get('alert')}"


# --- non-finite float exposition (Prometheus wants +Inf/-Inf/NaN, not inf/nan) ---
def test_format_value_non_finite_uses_prometheus_spelling():
    from src.api.metrics import _format_value
    assert _format_value(float("inf")) == "+Inf"
    assert _format_value(float("-inf")) == "-Inf"
    assert _format_value(float("nan")) == "NaN"
    # finite floats keep their plain rendering; ints/bools unchanged.
    assert _format_value(42.5) == "42.5"
    assert _format_value(7) == "7"
    assert _format_value(True) == "1"


def test_clock_step_non_finite_setting_renders_valid_sample(tmp_path):
    # A foreign write of a non-finite clock-step string must still yield a valid
    # exposition line (+Inf), never Python's "inf" which breaks Prometheus parsing.
    s = _settings(tmp_path)
    app = create_app(s)
    with TestClient(app) as client:
        _set_setting(s.db_path, "curator_clock_step_seconds", "inf")
        body = _scrape(client)
        assert "curator_clock_step_seconds +Inf" in body
        assert "curator_clock_step_seconds inf" not in body
