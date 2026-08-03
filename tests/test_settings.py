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


# --- enrollment knobs: bounds, not silent subsystem switches -----------------
@pytest.mark.parametrize(
    "var,value,why",
    [
        # 0 refuses every /ext handshake BEFORE accept(): the whole fleet drops out of
        # curation and the only trace is a rejections counter.
        ("ENROLL_PREAUTH_MAX", "0", "kills /ext"),
        ("ENROLL_PREAUTH_MAX", "-1", "kills /ext"),
        # 0 refuses every enroll_request with {reason:capacity}: no browser can ever be
        # added, and nothing in the logs names the cause.
        ("ENROLL_MAX_PENDING", "0", "kills enrollment"),
        # 0 expires every request the instant it is filed.
        ("ENROLL_REQUEST_TTL_MIN", "0", "requests expire immediately"),
        # 0 arms an already-closed window (arm_enroll_window silently floors it to 1).
        ("ENROLL_WINDOW_MIN", "0", "window is closed on arrival"),
        # Above the clamp the configured value is silently reduced at arm time, so the
        # config says one thing and the service does another.
        ("ENROLL_WINDOW_MIN", "525600", "silently clamped"),
        # 0 invalidates every session the moment it is minted.
        ("ADMIN_SESSION_TTL_MIN", "0", "no session can ever be valid"),
    ],
)
def test_out_of_range_enrollment_knobs_fail_at_startup(monkeypatch, var, value, why):
    """Each of these values SILENTLY disables a subsystem rather than erring.

    Project convention (AGENTS.md): a misconfigured value fails at startup instead of
    producing a service that looks healthy and does nothing. These four knobs arrived
    unbounded, so a typo bought a running curator with /ext shut, enrolment impossible, or
    every request expiring on arrival — each visible only as an absence. Reddens if a
    bound is dropped: the Settings object builds and the process starts.
    """
    _base_env(monkeypatch)
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_enroll_window_max_is_the_arming_clamp(monkeypatch):
    # The upper bound on ENROLL_WINDOW_MIN must BE the clamp arm_enroll_window applies —
    # one number, imported, not two that drift. Redden: change either side alone.
    from src.curator.enroll import ENROLL_WINDOW_MAX_MIN

    assert Settings.model_fields["enroll_window_min"].metadata
    _base_env(monkeypatch)
    monkeypatch.setenv("ENROLL_WINDOW_MIN", str(ENROLL_WINDOW_MAX_MIN))
    assert Settings(_env_file=None).enroll_window_min == ENROLL_WINDOW_MAX_MIN
    monkeypatch.setenv("ENROLL_WINDOW_MIN", str(ENROLL_WINDOW_MAX_MIN + 1))
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_request_ttl_must_outlast_the_enrollment_window(monkeypatch):
    """A request has to survive the window it was filed in — now a HARD requirement.

    Approval is gated on the window being open (src.api.admin.approve), so with
    ENROLL_REQUEST_TTL_MIN < ENROLL_WINDOW_MIN a request filed at the start of a window
    expires BEFORE the window closes: the operator's approve answers 404 for a row the
    console was showing a moment ago (both surfaces apply the same read-time TTL filter).
    That is a configuration that cannot work, so it fails at startup. Reddens if the
    cross-field validator is removed.
    """
    _base_env(monkeypatch)
    monkeypatch.setenv("ENROLL_WINDOW_MIN", "30")
    monkeypatch.setenv("ENROLL_REQUEST_TTL_MIN", "29")
    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None)
    assert "ENROLL_WINDOW_MIN" in str(ei.value)
    assert any(err["loc"] == ("enroll_request_ttl_min",) for err in ei.value.errors())

    # Equal is fine (the request lasts exactly as long as the window), and the shipping
    # defaults are comfortably clear of the boundary.
    monkeypatch.setenv("ENROLL_REQUEST_TTL_MIN", "30")
    assert Settings(_env_file=None).enroll_request_ttl_min == 30
    monkeypatch.delenv("ENROLL_WINDOW_MIN")
    monkeypatch.delenv("ENROLL_REQUEST_TTL_MIN")
    defaults = Settings(_env_file=None)
    assert defaults.enroll_request_ttl_min >= defaults.enroll_window_min
