"""Application configuration — the single source of truth for every ENV knob.

Every value comes from the environment (or `.env`); nothing is hardcoded. The
table below mirrors §4 "Конфигурация (ENV)" of docs/architecture.md verbatim:
non-secret tunables carry the §4 defaults, self-hosted/infra paths carry a
dev-friendly default under data/, and the two tokens (ADMIN_TOKEN, METRICS_TOKEN)
are REQUIRED with no default and reject an empty/blank string as well as a missing
variable (§4: "обязателен, пустой = отказ старта").
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config_errors import load_settings_or_exit


class Settings(BaseSettings):
    # --- Required tokens: no default; missing OR empty/blank fails at startup ---
    # ADMIN_TOKEN opens /admin (the enrollment console, §13), /api/* (as the
    # human/agent caller) and /mcp; /ext and /api/* also accept a per-instance
    # secretHash under enrollment (§13 — no shared token anymore). METRICS_TOKEN is a
    # separate read-only token for /metrics (§12: it lives in git plaintext scrape
    # configs, so it must never be able to touch anything but /metrics), and ADMIN_TOKEN
    # must DIFFER from it. Both are validated below.
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
    # Lifetime of a PENDING enroll_request (§13, acceptance 12). A request is filtered out
    # of GET /admin/enroll/requests once its FROZEN first_seen_at is older than this, and a
    # frequent sweep (TICK_MS, ~60s) physically deletes it — so a stale/abandoned request
    # self-clears within TTL+~60s instead of lingering in the operator list forever. 60
    # minutes is chosen as: (a) comfortably LONGER than the enrollment window
    # (ENROLL_WINDOW_MIN, 10 min) so an operator who opens a window always has a live
    # request to approve — an approvable request must outlast the window; (b) long enough
    # that a human noticing the request and approving it is unhurried; yet (c) bounded, so a
    # copied/abandoned install's request does not sit in the pre-auth list indefinitely
    # (the same self-clearing discipline the pending-cap and window give the pre-auth
    # surface). Also equals the window's own MAX (ENROLL_WINDOW_MAX_MIN=60), so a request
    # cannot expire under even a maximally-armed window.
    enroll_request_ttl_min: int = 60
    # Lifetime of an /admin HTML-console browser SESSION (§13, issue #36). The login cookie
    # carries a random id (never the ADMIN_TOKEN); this is how long that id stays valid in
    # the in-memory session store before a re-login is required. Enforced SERVER-side (the
    # cookie Max-Age is only a browser hint), and a rotated ADMIN_TOKEN invalidates every
    # session immediately regardless of this. 720 minutes = 12h: an unhurried operator
    # workday, bounded so a walked-away session does not stay open indefinitely.
    admin_session_ttl_min: int = 720
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

    @field_validator("metrics_token", "admin_token")
    @classmethod
    def _reject_blank_token(cls, v: str) -> str:
        # Field(min_length=1) already rejects a missing var and the empty string,
        # but a whitespace-only value ("   ") would slip through — reject it too so
        # a blank token can never silently become a live credential.
        if v is None or not v.strip():
            raise ValueError("must not be empty or blank")
        return v

    @field_validator("admin_token")
    @classmethod
    def _must_differ_from_metrics_token(cls, v: str, info) -> str:
        # ADMIN_TOKEN opens /admin (enrollment approvals, revocation — §13), /api/* (as
        # the human/agent caller) and /mcp. METRICS_TOKEN is the plaintext scrape
        # credential that lives in git (§12). If ADMIN_TOKEN equals METRICS_TOKEN, that
        # plaintext scrape credential now opens /admin, /api/* and /mcp too — a
        # "decorative separation" that looks configured while protecting nothing. It
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
