"""Application configuration — the single source of truth for every ENV knob.

Every value comes from the environment (or `.env`); nothing is hardcoded. The
table below mirrors §4 "Конфигурация (ENV)" of docs/architecture.md verbatim:
non-secret tunables carry the §4 defaults, self-hosted/infra paths carry a
dev-friendly default under data/, and the three tokens are REQUIRED with no default
and reject an empty/blank string as well as a missing variable (§4:
"обязателен, пустой = отказ старта").
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config_errors import load_settings_or_exit


class Settings(BaseSettings):
    # --- Required tokens: no default; missing OR empty/blank fails at startup ---
    # EXT_TOKEN opens /ext, /api/* and /mcp; METRICS_TOKEN is a separate read-only
    # token for /metrics (§12: it lives in git plaintext scrape configs, so it must
    # never be able to touch anything but /metrics). ADMIN_TOKEN opens /admin (the
    # enrollment console, §13) and must differ from METRICS_TOKEN for the same reason
    # METRICS_TOKEN must differ from EXT_TOKEN. All three are validated below.
    ext_token: str = Field(min_length=1)
    metrics_token: str = Field(min_length=1)
    admin_token: str = Field(min_length=1)

    # --- §4 tunables (non-secret): defaults are the architecture's numbers -------
    idle_minutes: int = 60
    pass_interval_min: int = 5
    tick_ms: int = 60000
    heartbeat_ms: int = 15000
    cmd_timeout_ms: int = 20000
    snapshot_timeout_ms: int = 10000
    lease_ttl_ms: int = 600000
    restore_exemption_min: int = 120
    pause_default_min: int = 60
    incomplete_after_min: int = 15
    self_nav_limit: int = 10
    state_fresh_ms: int = 3000
    quarantine_ttl_min: int = 1440
    actions_retention_days: int = 90
    js_audit_retention_days: int = 730
    main_instance_id: str = "main"
    protocol_version: int = 1
    # Enrollment window length (§13). A per-open window during which an operator can
    # approve pending enroll requests; compared against "now" AT READ TIME (no timer),
    # so a restart neither silently closes nor leaves-open-forever an armed window.
    enroll_window_min: int = 10
    # Ceiling on the number of PENDING enroll_requests (§2). A not-yet-approved client's
    # enroll_request is refused with enroll_rejected{reason:capacity} once the pending
    # list is at this size, so a flood of anonymous enroll_requests cannot grow the
    # operator-facing list without bound. 64 is generous for a human-scale fleet while
    # still bounding the pre-auth list.
    enroll_max_pending: int = 64
    # Ceiling on simultaneously-open /ext sockets that have been accepted but have not
    # yet completed a hello/enroll (§2). Refused BEFORE accept() (a handshake rejection,
    # no TLS session), so a flood of opened-but-silent sockets cannot exhaust memory or
    # TLS sessions. A live authenticated connection releases its slot on a successful
    # hello, so this bounds only the pre-auth window, not the connected fleet.
    enroll_preauth_max: int = 128
    # Comma-separated allow-list for the extension's `hello.origin`. DEFAULT
    # EMPTY = accept any origin and log a one-time warning (the concrete
    # chrome-extension:// id is unknown until the extension/generator phases;
    # §12 tightens this later). When non-empty, a hello whose origin is not in
    # the list is rejected with reject_reason='origin'.
    ext_allowed_origins: str = ""

    # Path to the EXTERNAL restore-from-backup marker (§7 WARNING 2). DEFAULT EMPTY =
    # detection off — which the continuity fingerprint records as a state ("no marker
    # configured"), not as a missing component: an install that stays off never breaks,
    # but switching detection on or off later is a break like any other fingerprint
    # config change (deliberate — a silent switch-OFF would drop restore detection with
    # no signal; see clock.is_continuity_break). When set, it must point at a small file
    # on a
    # volume that is NOT part of the DB backup, holding a fresh uuid written on every
    # restore — see src/curator/clock.read_restore_marker for the operator contract and
    # deploy/DEPLOY.md for the procedure. No default path: an invented one would either
    # never exist (silently disabling the detector) or accidentally sit inside the
    # backup (detecting nothing).
    restore_marker_path: str = ""

    # --- Non-secret infra defaults ----------------------------------------------
    log_level: str = "INFO"
    db_path: str = "data/curator.db"  # all mutable state lives under data/
    # Backups mount on a volume separate from the DB (§12); default under data/ for
    # dev, overridable via BACKUP_DIR in prod.
    backup_dir: str = "data/backups"
    host: str = "0.0.0.0"
    port: int = 8000

    @field_validator("ext_token", "metrics_token", "admin_token")
    @classmethod
    def _reject_blank_token(cls, v: str) -> str:
        # Field(min_length=1) already rejects a missing var and the empty string,
        # but a whitespace-only value ("   ") would slip through — reject it too so
        # a blank token can never silently become a live credential.
        if v is None or not v.strip():
            raise ValueError("must not be empty or blank")
        return v

    @field_validator("metrics_token")
    @classmethod
    def _must_differ_from_ext_token(cls, v: str, info) -> str:
        # The ONLY reason METRICS_TOKEN exists is its storage location (§12): the scrape
        # config lives in git as plaintext, so the credential that goes there must open
        # nothing but /metrics. Setting it to the same string as EXT_TOKEN publishes the
        # key to /ext, /api/* and /mcp in that same plaintext file — the separation
        # becomes decorative while looking configured. Fail at startup instead (project
        # convention: a misconfigured credential never starts).
        # ``ext_token`` is declared first, so it is already validated in ``info.data``;
        # if it failed its own validation it is absent and there is nothing to compare.
        ext = info.data.get("ext_token")
        if ext is not None and v == ext:
            raise ValueError(
                "must differ from EXT_TOKEN — METRICS_TOKEN is the read-only /metrics "
                "credential that lives in a plaintext scrape config; reusing EXT_TOKEN "
                "there would expose /ext, /api/* and /mcp"
            )
        return v

    @field_validator("admin_token")
    @classmethod
    def _must_differ_from_metrics_token(cls, v: str, info) -> str:
        # ADMIN_TOKEN opens /admin (enrollment approvals, revocation — §13). METRICS_TOKEN
        # is the plaintext scrape credential that lives in git (§12). If ADMIN_TOKEN equals
        # METRICS_TOKEN, that plaintext scrape credential now opens /admin too — the same
        # "decorative separation" failure guarded between EXT_TOKEN and METRICS_TOKEN. It
        # would also collapse the compare_digest bearer check: an empty/whitespace Bearer
        # never matches a non-empty token, but a credential SHARED with metrics does. Fail
        # at startup (project convention: a misconfigured credential never starts).
        # ``metrics_token`` is declared before ``admin_token``, so it is already validated
        # in ``info.data``; if it failed its own validation it is absent and there is
        # nothing to compare against.
        metrics = info.data.get("metrics_token")
        if metrics is not None and v == metrics:
            raise ValueError(
                "must differ from METRICS_TOKEN — ADMIN_TOKEN opens /admin (enrollment, "
                "revocation); METRICS_TOKEN is the read-only credential that lives in a "
                "plaintext scrape config, so reusing it here would expose /admin"
            )
        return v

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Build settings with clear startup errors: a missing/invalid variable prints a
# readable message naming the env var and exits, instead of a raw pydantic
# traceback. The same helper is reused by any other entrypoint (e.g. an MCP server).
settings = load_settings_or_exit(Settings)
