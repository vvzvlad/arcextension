import pytest
from pydantic import ValidationError

from src.settings import Settings


def _base_env(monkeypatch):
    """Set the two required tokens so only the field under test is unset."""
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")


def test_loads_defaults_from_section4(monkeypatch):
    _base_env(monkeypatch)
    s = Settings(_env_file=None)
    # Required tokens come through.
    assert s.metrics_token == "metrics-secret"
    assert s.admin_token == "admin-secret"
    # §4 tunable defaults.
    assert s.idle_minutes == 60
    assert s.pass_interval_min == 5
    assert s.tick_ms == 60000
    assert s.heartbeat_ms == 15000
    assert s.cmd_timeout_ms == 20000
    assert s.snapshot_timeout_ms == 10000
    assert s.lease_ttl_ms == 600000
    assert s.restore_exemption_min == 120
    assert s.pause_default_min == 60
    assert s.incomplete_after_min == 15
    assert s.self_nav_limit == 10
    assert s.state_fresh_ms == 3000
    assert s.quarantine_ttl_min == 1440
    assert s.actions_retention_days == 90
    assert s.js_audit_retention_days == 730
    assert s.main_instance_id == "main"
    assert s.protocol_version == 1
    # Infra defaults.
    assert s.log_level == "INFO"
    assert s.db_path == "data/curator.db"
    assert s.backup_dir == "data/backups"
    assert s.host == "0.0.0.0"
    assert s.port == 8000


def test_shared_ext_secret_field_is_gone(monkeypatch):
    # Enrollment (§13) removed the shared /ext token entirely (#37). Settings must carry
    # no such field — /ext and /api/* authenticate by a per-instance secretHash and
    # ADMIN_TOKEN, never a config token. Redden: re-add the field.
    #
    # The removed field name is spelled INDIRECTLY here on purpose: #37 acceptance 1 is a
    # repo-wide purge (`grep -rn` for the literal must find nothing but git history), and
    # this assert-it-is-gone test must not itself reintroduce the literal.
    removed_field = "ext" + "_token"
    _base_env(monkeypatch)
    s = Settings(_env_file=None)
    assert not hasattr(s, removed_field)
    assert removed_field not in Settings.model_fields


def test_env_overrides_tunables(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("IDLE_MINUTES", "30")
    monkeypatch.setenv("MAIN_INSTANCE_ID", "primary")
    s = Settings(_env_file=None)
    assert s.idle_minutes == 30
    assert s.main_instance_id == "primary"


def test_missing_metrics_token_fails(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_metrics_token_fails(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_metrics_token_fails(monkeypatch):
    # A whitespace-only token is just as unusable as an empty one.
    monkeypatch.setenv("METRICS_TOKEN", "   ")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_distinct_tokens_still_load(monkeypatch):
    _base_env(monkeypatch)
    assert Settings(_env_file=None).metrics_token == "metrics-secret"


# --- ADMIN_TOKEN (§13): required, blank-rejected, must differ from METRICS_TOKEN ---
def test_admin_token_loads(monkeypatch):
    _base_env(monkeypatch)
    assert Settings(_env_file=None).admin_token == "admin-secret"


def test_missing_admin_token_fails(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_admin_token_fails(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ADMIN_TOKEN", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_admin_token_fails(monkeypatch):
    # A whitespace-only token is just as unusable as an empty one.
    _base_env(monkeypatch)
    monkeypatch.setenv("ADMIN_TOKEN", "   ")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_admin_token_equal_to_metrics_fails_at_startup(monkeypatch):
    # §13/§12: ADMIN_TOKEN opens /admin (enrollment approvals, revocation). METRICS_TOKEN
    # lives in a plaintext scrape config in git; making ADMIN_TOKEN equal to it would let
    # that read-only scrape credential open /admin. A misconfigured credential must fail
    # at startup, and the error must attach to admin_token so the env var is named.
    monkeypatch.setenv("METRICS_TOKEN", "shared-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "shared-secret")
    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None)
    msg = str(ei.value)
    assert "METRICS_TOKEN" in msg
    assert any(err["loc"] == ("admin_token",) for err in ei.value.errors())


def test_enroll_window_min_default_and_override(monkeypatch):
    _base_env(monkeypatch)
    assert Settings(_env_file=None).enroll_window_min == 10
    monkeypatch.setenv("ENROLL_WINDOW_MIN", "25")
    assert Settings(_env_file=None).enroll_window_min == 25


def test_restore_marker_path_defaults_to_unset(monkeypatch):
    # §7 WARNING 2: no default path — an invented one would either never exist (silently
    # disabling the detector) or sit inside the backup (detecting nothing). Empty means
    # "not configured", which is the pre-existing behaviour.
    _base_env(monkeypatch)
    assert Settings(_env_file=None).restore_marker_path == ""
    monkeypatch.setenv("RESTORE_MARKER_PATH", "/app/state/restore-marker")
    assert Settings(_env_file=None).restore_marker_path == "/app/state/restore-marker"
