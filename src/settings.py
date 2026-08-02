"""Application configuration — the single source of truth for every ENV knob.

Every value comes from the environment (or `.env`); nothing is hardcoded. The
table below mirrors §4 "Конфигурация (ENV)" of docs/architecture.md verbatim:
non-secret tunables carry the §4 defaults, self-hosted/infra paths carry a
dev-friendly default under data/, and the two tokens are REQUIRED with no default
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
    # never be able to touch anything but /metrics). Both are validated below.
    ext_token: str = Field(min_length=1)
    metrics_token: str = Field(min_length=1)

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
    # Comma-separated allow-list for the extension's `hello.origin`. DEFAULT
    # EMPTY = accept any origin and log a one-time warning (the concrete
    # chrome-extension:// id is unknown until the extension/generator phases;
    # §12 tightens this later). When non-empty, a hello whose origin is not in
    # the list is rejected with reject_reason='origin'.
    ext_allowed_origins: str = ""

    # --- Non-secret infra defaults ----------------------------------------------
    log_level: str = "INFO"
    db_path: str = "data/curator.db"  # all mutable state lives under data/
    # Backups mount on a volume separate from the DB (§12); default under data/ for
    # dev, overridable via BACKUP_DIR in prod.
    backup_dir: str = "data/backups"
    host: str = "0.0.0.0"
    port: int = 8000

    @field_validator("ext_token", "metrics_token")
    @classmethod
    def _reject_blank_token(cls, v: str) -> str:
        # Field(min_length=1) already rejects a missing var and the empty string,
        # but a whitespace-only value ("   ") would slip through — reject it too so
        # a blank token can never silently become a live credential.
        if v is None or not v.strip():
            raise ValueError("must not be empty or blank")
        return v

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Build settings with clear startup errors: a missing/invalid variable prints a
# readable message naming the env var and exits, instead of a raw pydantic
# traceback. The same helper is reused by any other entrypoint (e.g. an MCP server).
settings = load_settings_or_exit(Settings)
