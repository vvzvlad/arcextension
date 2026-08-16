import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.settings import EXT_WAIT_MAX_TIMEOUT_MS, Settings


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
    assert s.execute_js_max_timeout_ms == 30000
    assert s.snapshot_timeout_ms == 10000
    assert s.lease_ttl_ms == 600000
    assert s.restore_exemption_min == 120
    assert s.max_actions_per_pass == 20
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


def test_max_actions_per_pass_env_and_floor(monkeypatch):
    # §7: the ONLY mass-action brake. Env-tunable; ge=1 because a 0 threshold would
    # defer every non-empty pass forever (config that means "never run").
    _base_env(monkeypatch)
    assert Settings(_env_file=None).max_actions_per_pass == 20
    monkeypatch.setenv("MAX_ACTIONS_PER_PASS", "5")
    assert Settings(_env_file=None).max_actions_per_pass == 5
    monkeypatch.setenv("MAX_ACTIONS_PER_PASS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- build identity: stamped by the image build, honest when it is not -------
def test_build_revision_comes_from_the_build_and_defaults_to_unknown(monkeypatch):
    """BUILD_REVISION is baked by the Dockerfile (`ARG` -> `ENV`, fed by CI from
    `github.sha`); unset, it must be the word "unknown" and must NOT stop the service.

    The two halves are the whole contract. Unset has to keep working — a local `make run`
    has no sha to bake, and a build identity is not worth refusing to start over (unlike
    the tokens, where a missing value is a security hole). Set has to come through
    verbatim — a revision that is mangled is worse than none, because it is believed.
    """
    _base_env(monkeypatch)
    monkeypatch.delenv("BUILD_REVISION", raising=False)
    assert Settings(_env_file=None).build_revision == "unknown"

    monkeypatch.setenv("BUILD_REVISION", "0f2c9a1b3d4e5f60718293a4b5c6d7e8f9012345")
    assert (
        Settings(_env_file=None).build_revision
        == "0f2c9a1b3d4e5f60718293a4b5c6d7e8f9012345"
    )


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_build_revision_reads_as_unknown_not_as_an_empty_string(monkeypatch, value):
    """A build that declared the ARG but received no value yields `ENV BUILD_REVISION=`.

    That must normalize to the same "unknown" an unset variable gives: `"revision": ""` on
    /healthz reads as a broken endpoint, and a reader who cannot tell "no revision" from
    "the endpoint is buggy" is back to guessing — which is the failure this field exists to
    end. Not a startup failure either: the blank case is a build-pipeline slip, and
    refusing to start would turn a missing label into an outage.
    """
    _base_env(monkeypatch)
    monkeypatch.setenv("BUILD_REVISION", value)
    assert Settings(_env_file=None).build_revision == "unknown"


# --- enrollment knobs: bounds, not silent subsystem switches -----------------
@pytest.mark.parametrize(
    "var,value,why",
    [
        # 0 refuses every /ext handshake BEFORE accept(): the whole fleet drops out of
        # curation and the only trace is a rejections counter.
        ("ENROLL_PREAUTH_MAX", "0", "kills /ext"),
        ("ENROLL_PREAUTH_MAX", "-1", "kills /ext"),
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
    producing a service that looks healthy and does nothing. These knobs arrived unbounded,
    so a typo bought a running curator with /ext shut or a window closed on arrival — each
    visible only as an absence. Reddens if a bound is dropped: the Settings object builds
    and the process starts.

    Two knobs left this list rather than losing their bound: ENROLL_MAX_PENDING and
    ENROLL_REQUEST_TTL_MIN are GONE with the pending-request table (§6) — see
    ``test_the_retired_enrollment_knobs_are_gone`` below.
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


def test_the_retired_enrollment_knobs_are_gone_and_a_leftover_value_is_harmless(monkeypatch):
    """ENROLL_MAX_PENDING / ENROLL_REQUEST_TTL_MIN no longer exist, and a `.env` that
    still sets them must not stop the service.

    They governed the ``enroll_requests`` table — a ceiling on the pending list and a TTL
    for its rows — and enrolment is one step now (§6), so neither has anything to bound.
    Their cross-validator (TTL >= ENROLL_WINDOW_MIN, which existed because an approve had
    to happen while the window was still open) went with them.

    The second half is the upgrade path: every deployed `.env` still carries both
    variables, and `extra="ignore"` is what keeps that from being a failed startup on the
    release that removes them. Reddens if the fields are quietly re-added (the attribute
    check) or if the model is switched to `extra="forbid"` (the leftover env fails).
    """
    _base_env(monkeypatch)
    monkeypatch.setenv("ENROLL_MAX_PENDING", "64")
    monkeypatch.setenv("ENROLL_REQUEST_TTL_MIN", "60")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "enroll_max_pending")
    assert not hasattr(settings, "enroll_request_ttl_min")
    # A nonsense leftover is equally harmless — it is no longer a knob at all.
    monkeypatch.setenv("ENROLL_REQUEST_TTL_MIN", "0")
    assert Settings(_env_file=None).enroll_window_min == 10


def test_execute_js_max_timeout_is_configurable_and_refuses_a_useless_value(monkeypatch):
    """The ceiling a caller-named per-command budget is clamped to.

    It exists because ONE global CMD_TIMEOUT_MS cannot serve both an ordinary command and
    a verb that waits by definition: raising the global would hand every command a
    30-second wedge budget, which is how one hung tab stalls a whole pass.

    0 / negative is a startup failure rather than a silent floor: it would make every
    async execute_js and every wait_for fail instantly, i.e. config that quietly disables
    the feature it names.
    """
    _base_env(monkeypatch)
    monkeypatch.setenv("EXECUTE_JS_MAX_TIMEOUT_MS", "45000")
    assert Settings(_env_file=None).execute_js_max_timeout_ms == 45000
    for bad in ("0", "-1"):
        monkeypatch.setenv("EXECUTE_JS_MAX_TIMEOUT_MS", bad)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
    monkeypatch.setenv("EXECUTE_JS_MAX_TIMEOUT_MS", "not-a-number")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_execute_js_max_timeout_cannot_exceed_what_the_extension_will_honour(monkeypatch):
    """A budget the browser will not honour is refused at STARTUP, never silently reduced.

    The extension clamps every polling verb at its own WAIT_MAX_TIMEOUT_MS (60 s: a worker
    parked in a poll loop is a worker not running its tick). Without the upper bound, an
    operator writing 120000 would be told 120000 by every message and handed 60 s — config
    that means something other than it says, which is the failure mode AGENTS.md answers
    with "missing/😖 ENV -> fail at startup".
    """
    _base_env(monkeypatch)
    monkeypatch.setenv("EXECUTE_JS_MAX_TIMEOUT_MS", str(EXT_WAIT_MAX_TIMEOUT_MS))
    assert Settings(_env_file=None).execute_js_max_timeout_ms == EXT_WAIT_MAX_TIMEOUT_MS
    monkeypatch.setenv("EXECUTE_JS_MAX_TIMEOUT_MS", str(EXT_WAIT_MAX_TIMEOUT_MS + 1))
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_extension_wait_ceiling_mirror_is_real():
    """``EXT_WAIT_MAX_TIMEOUT_MS`` equals ``WAIT_MAX_TIMEOUT_MS`` in constants.js.

    Duplicated by construction — the other side is JavaScript in a browser and there is no
    shared artifact to import — so the promise is only worth what a test makes of it. Same
    predicament, and same remedy, as the CMD_*/ERR_* wire strings in test_ext_protocol.py:
    parse the JS. Lower the extension's ceiling alone and the service would keep accepting
    budgets it silently cannot deliver.
    """
    constants_js = (
        Path(__file__).resolve().parent.parent / "extension" / "src" / "constants.js"
    ).read_text(encoding="utf-8")
    m = re.search(r"^export const WAIT_MAX_TIMEOUT_MS\s*=\s*(\d+);", constants_js, re.MULTILINE)
    assert m, "WAIT_MAX_TIMEOUT_MS not found in constants.js (moved or reformatted?)"
    assert int(m.group(1)) == EXT_WAIT_MAX_TIMEOUT_MS
