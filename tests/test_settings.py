import pytest
from pydantic import ValidationError

from src.settings import Settings


def _base_env(monkeypatch):
    """Set the two required tokens so only the field under test is unset."""
    monkeypatch.setenv("EXT_TOKEN", "ext-secret")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret")


def test_loads_defaults_from_section4(monkeypatch):
    _base_env(monkeypatch)
    s = Settings(_env_file=None)
    # Required tokens come through.
    assert s.ext_token == "ext-secret"
    assert s.metrics_token == "metrics-secret"
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


def test_env_overrides_tunables(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("IDLE_MINUTES", "30")
    monkeypatch.setenv("MAIN_INSTANCE_ID", "primary")
    s = Settings(_env_file=None)
    assert s.idle_minutes == 30
    assert s.main_instance_id == "primary"


def test_missing_ext_token_fails(monkeypatch):
    monkeypatch.delenv("EXT_TOKEN", raising=False)
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_missing_metrics_token_fails(monkeypatch):
    monkeypatch.setenv("EXT_TOKEN", "ext-secret")
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_ext_token_fails(monkeypatch):
    # §4: "пустой = отказ старта" — an empty string must fail, not only a missing var.
    monkeypatch.setenv("EXT_TOKEN", "")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_ext_token_fails(monkeypatch):
    # A whitespace-only token is just as unusable as an empty one.
    monkeypatch.setenv("EXT_TOKEN", "   ")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_metrics_token_fails(monkeypatch):
    monkeypatch.setenv("EXT_TOKEN", "ext-secret")
    monkeypatch.setenv("METRICS_TOKEN", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_identical_tokens_fail_at_startup(monkeypatch):
    # §12: METRICS_TOKEN exists ONLY because of where it is stored — a scrape config in
    # git, in plaintext. Making it equal to EXT_TOKEN publishes /ext, /api/* and /mcp in
    # that same file while the config still LOOKS separated. Project convention
    # (AGENTS.md): a misconfigured credential fails at startup.
    monkeypatch.setenv("EXT_TOKEN", "same-secret")
    monkeypatch.setenv("METRICS_TOKEN", "same-secret")
    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None)
    msg = str(ei.value)
    assert "EXT_TOKEN" in msg  # the message names what to change
    # And the error is attached to METRICS_TOKEN, so load_settings_or_exit prints the
    # env var name rather than a bare "?".
    assert any(err["loc"] == ("metrics_token",) for err in ei.value.errors())


def test_distinct_tokens_still_load(monkeypatch):
    _base_env(monkeypatch)
    assert Settings(_env_file=None).metrics_token == "metrics-secret"


def test_restore_marker_path_defaults_to_unset(monkeypatch):
    # §7 WARNING 2: no default path — an invented one would either never exist (silently
    # disabling the detector) or sit inside the backup (detecting nothing). Empty means
    # "not configured", which is the pre-existing behaviour.
    _base_env(monkeypatch)
    assert Settings(_env_file=None).restore_marker_path == ""
    monkeypatch.setenv("RESTORE_MARKER_PATH", "/app/state/restore-marker")
    assert Settings(_env_file=None).restore_marker_path == "/app/state/restore-marker"
