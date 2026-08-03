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
# The armed-window ceiling lives with the arming code (src.curator.enroll, which imports
# only sqlite3 + src.db.settings_store — no cycle back here). Imported rather than copied
# so ENROLL_WINDOW_MIN's bound and the clamp arm_enroll_window applies can never drift:
# a value above the clamp would be silently reduced at arm time, i.e. config that lies.
from src.curator.enroll import ENROLL_WINDOW_MAX_MIN

# What ``build_revision`` says when the image was built without a revision (a local
# `make run`, a hand-rolled `docker build` with no `--build-arg`). A word, never an empty
# string: "unknown" is an answer ("this build cannot tell you"), "" reads as a bug in
# whoever printed it.
UNKNOWN_REVISION = "unknown"


class Settings(BaseSettings):
    # --- Required tokens: no default; missing OR empty/blank fails at startup ---
    # ADMIN_TOKEN opens /admin (the enrollment console, §13), /api/* (as the
    # human/agent caller) and /mcp; /ext and /api/* also accept a per-instance SECRET
    # under enrollment (§13 — no shared token anymore). That secret is credential model
    # OPTION A: the client generates it, sends the RAW value over TLS on every hello /
    # /api Bearer, and the SERVER hashes it on receipt (``queries.sha256_hex``) — only the
    # sha256 is ever stored, so a DB-only leak yields hashes, not usable credentials. It is
    # NOT the rejected option B ("the client sends a secretHash"), under which the stored
    # value would itself be the bearer credential and a DB leak would hand over the fleet.
    # METRICS_TOKEN is a separate read-only token for /metrics (§12: it lives in git
    # plaintext scrape configs, so it must never be able to touch anything but /metrics),
    # and ADMIN_TOKEN must DIFFER from it. Both are validated below.
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
    # Bounded, like every enrollment knob below: 0/negative would arm an already-closed
    # window (arm_enroll_window silently floors it to 1) and a value above the clamp would
    # be silently reduced — both are config that does not mean what it says.
    enroll_window_min: int = Field(default=10, ge=1, le=ENROLL_WINDOW_MAX_MIN)
    # ENROLL_MAX_PENDING and ENROLL_REQUEST_TTL_MIN are GONE, together with the
    # `enroll_requests` table they governed. Enrolment is one step now (§6): an
    # enroll_request with a valid code into an open window creates the active instance
    # immediately, so there is no pending list to cap and no stale row to age out. Their
    # cross-validator (TTL >= ENROLL_WINDOW_MIN, which existed because an approve was gated
    # on the window still being open) went with them: nothing outlives the frame anymore.
    # Both are read from `.env` as `extra="ignore"` leftovers without error, which is the
    # right behaviour for a knob that stopped meaning anything.
    #
    # Lifetime of an /admin HTML-console browser SESSION (§13, issue #36). The login cookie
    # carries a random id (never the ADMIN_TOKEN); this is how long that id stays valid in
    # the in-memory session store before a re-login is required. Enforced SERVER-side (the
    # cookie Max-Age is only a browser hint), and a rotated ADMIN_TOKEN invalidates every
    # session immediately regardless of this. 720 minutes = 12h: an unhurried operator
    # workday, bounded so a walked-away session does not stay open indefinitely.
    admin_session_ttl_min: int = Field(default=720, ge=1)
    # Ceiling on simultaneously-open /ext sockets that have been accepted but have not
    # yet completed a hello/enroll (§2). Refused BEFORE accept() (a handshake rejection,
    # no TLS session), so a flood of opened-but-silent sockets cannot exhaust memory or
    # TLS sessions. A live authenticated connection releases its slot on a successful
    # hello, so this bounds only the pre-auth window, not the connected fleet.
    # ge=1: 0 refuses EVERY /ext handshake before accept() — the whole fleet drops out of
    # curation with only `curator_auth_rejections_total{reason="capacity"}` to show for it.
    enroll_preauth_max: int = Field(default=128, ge=1)
    # Path to the EXTERNAL restore-from-backup marker (§7 WARNING 2). DEFAULT EMPTY =
    # detection off — which the continuity fingerprint records as a state ("no marker
    # configured"), not as a missing component: an install that stays off never breaks,
    # but switching detection on or off later is a break like any other fingerprint
    # config change (deliberate — a silent switch-OFF would drop restore detection with
    # no signal; see clock.is_continuity_break). When set, it must point at a small file
    # that is NOT the DB and NOT inside the backup copies, holding a fresh uuid
    # written on every restore — see src/curator/clock.read_restore_marker for the
    # operator contract and deploy/DEPLOY.md for the procedure. In prod the shipped
    # compose file points it at /app/data/restore/continuity-marker: the same volume as
    # the DB, but its own file (the container entrypoint creates it once and never
    # rewrites it). No default path: an invented one would either never exist (silently
    # disabling the detector) or accidentally sit inside the backup (detecting nothing).
    restore_marker_path: str = ""

    # --- Non-secret infra defaults ----------------------------------------------
    log_level: str = "INFO"
    db_path: str = "data/curator.db"  # all mutable state lives under data/
    # Backups live under data/ beside the DB — one volume for all mutable state, in
    # prod BACKUP_DIR=/app/data/backups (deploy/DEPLOY.md §5). They therefore share
    # free space with the DB; `curator-backup-stale` is the alert that surfaces a
    # copy that stopped landing.
    backup_dir: str = "data/backups"
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Build identity (NOT a knob) --------------------------------------------
    # WHICH REVISION IS RUNNING. The service is deployed as a ghcr image and updated by
    # watchtower, so «кнопка не работает» has two indistinguishable causes — the code is
    # broken, or the code is still the old one — and neither the logs nor /healthz used to
    # separate them. This field is that separator, and it is reported by /healthz (public,
    # no token) and printed in the /admin console.
    #
    # It is set ONCE, BY THE IMAGE BUILD: `Dockerfile` declares `ARG BUILD_REVISION` and
    # bakes it into an `ENV`, CI passes `--build-arg BUILD_REVISION=${{ github.sha }}`.
    # It cannot be computed at runtime — the container has no git and no repository — and
    # deriving it from the working tree would describe the CHECKOUT, not the deployed
    # image, i.e. it would lie exactly when it matters.
    #
    # DO NOT set BUILD_REVISION in `.env` or in docker-compose. It is read from the
    # environment only because that is how the Dockerfile bakes it in; an operator-supplied
    # value makes the service claim a revision it is not running, which is worse than
    # having none at all.
    #
    # No default/no-start rule here (unlike the tokens): a local `make run` has no sha to
    # bake and must still start, so the default is the honest :data:`UNKNOWN_REVISION`.
    build_revision: str = UNKNOWN_REVISION

    @field_validator("build_revision")
    @classmethod
    def _blank_revision_is_unknown(cls, v: str) -> str:
        # A build that declared the ARG but got no value produces `ENV BUILD_REVISION=`,
        # i.e. an empty string — which would surface as `"revision": ""` and read as a bug
        # in the endpoint rather than as "this build has no revision". Normalize it (and a
        # whitespace-only value) to the same word an unset variable gives.
        return v.strip() or UNKNOWN_REVISION

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
