"""Ordered migration steps. The schema here is the canon copied verbatim from
docs/architecture.md §4 — column names, types, defaults and primary keys must
match §4 exactly (the version-1 schema mirrors the §4 base tables; the version-2
enrollment delta mirrors §4's "Миграция 2 — enrollment" block). The migration
runner (migrations.py) applies these steps under an explicit BEGIN IMMEDIATE
transaction each.

A step is ``(target_version: int, statements: list[str])``. Each statement is a
single DDL/DML operation; the runner executes them in order inside one
transaction and then sets ``PRAGMA user_version = target_version``.
"""

# --- Version 1: the full initial schema (§4) --------------------------------
_V1_STATEMENTS: list[str] = [
    # instances
    """
    CREATE TABLE instances (
        id TEXT PRIMARY KEY,                  -- instanceId from extension settings
        title TEXT,                           -- human-readable name, arrives in hello
        conn_epoch INTEGER NOT NULL DEFAULT 0,-- monotonic connection counter
        connected INTEGER NOT NULL DEFAULT 0,
        focused_window_id INTEGER,            -- instance's foreground window, NULL = none
        session_id TEXT,
        snapshot_at INTEGER,                  -- server time the request was sent
        last_seen_at INTEGER,
        reject_reason TEXT,                   -- reason of last rejection (auth/protocol)
        reject_at INTEGER,
        allow_execute_js INTEGER NOT NULL DEFAULT 0
    )
    """,
    # tabs
    """
    CREATE TABLE tabs (
        instance_id TEXT NOT NULL,
        tab_id INTEGER NOT NULL,
        window_id INTEGER,
        url TEXT,
        title TEXT,
        fav_icon_url TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 0,
        opened_at INTEGER NOT NULL,
        last_active_at INTEGER NOT NULL,
        age_unknown INTEGER NOT NULL DEFAULT 0,     -- age was assigned, not observed
        self_navigating INTEGER NOT NULL DEFAULT 0, -- page navigates itself (§5)
        audible INTEGER NOT NULL DEFAULT 0,         -- audible: close guard (§7)
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (instance_id, tab_id)
    )
    """,
    # windows
    """
    CREATE TABLE windows (
        instance_id TEXT NOT NULL,
        window_id INTEGER NOT NULL,
        type TEXT NOT NULL,                   -- normal | popup | app | devtools
        state TEXT,                           -- normal | minimized | maximized | fullscreen
        PRIMARY KEY (instance_id, window_id)
    )
    """,
    # rules
    """
    CREATE TABLE rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        singleton INTEGER NOT NULL DEFAULT 0,
        canonical_url TEXT,
        note TEXT,
        invalid INTEGER NOT NULL DEFAULT 0,   -- does not compile; excluded from matching
        created_at INTEGER NOT NULL
    )
    """,
    # actions
    """
    CREATE TABLE actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pass_id TEXT,                         -- pass id: lets a whole pass be rolled back
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        instance_from TEXT,
        instance_to TEXT,
        tab_id INTEGER,                       -- source tab
        session_id_from TEXT,                 -- source session at decision time
        tab_id_to INTEGER,                    -- copy created by phase A (§7 step 7)
        session_id_to TEXT,                   -- target session at ack time
        origin_action_id INTEGER,             -- phase B -> phase A row; restore -> close
        rule_id INTEGER,                      -- which rule decided; NULL for the drain
        rule_pattern TEXT,                    -- copy of the pattern: rule may be deleted
        -- WHY this row exists. On a row with status='deferred' this is the CAUSE and
        -- `reason` carries the aggregated COUNT, not a message — /metrics builds the
        -- `curator_deferred_total{cause}` label from this column, so the vocabulary is
        -- load-bearing rather than descriptive:
        --   pass routing : rule_home | unruled_drain | dedupe | singleton
        --   deferrals    : target_not_ready | dup_same_pass
        --   manual verbs : reset (§8) | undo (§10) | mcp_relocate (§11)
        decision TEXT,
        src_opened_at INTEGER,                -- source clock, inherited by the copy
        src_last_active_at INTEGER,
        src_age_unknown INTEGER NOT NULL DEFAULT 0,
        url TEXT,                             -- full, for the archive and restore
        url_norm TEXT,                        -- origin+path, for search and dedupe
        title TEXT,
        pinned INTEGER,
        reason TEXT,                          -- rejection reason or a counter on deferred
        initiator TEXT NOT NULL,              -- curator | mcp | user
        detail TEXT,                          -- payload by kind: on *_close - survivor
        restored_at INTEGER
    )
    """,
    "CREATE INDEX actions_ts       ON actions(ts)",
    "CREATE INDEX actions_pass     ON actions(pass_id)",
    "CREATE INDEX actions_url_norm ON actions(url_norm)",
    "CREATE INDEX actions_origin   ON actions(origin_action_id)",
    # passes
    """
    CREATE TABLE passes (                     -- fact of a pass: a pass with no
        pass_id TEXT PRIMARY KEY,             -- decisions writes no actions rows
        started_at INTEGER NOT NULL,
        finished_at INTEGER,
        ok INTEGER,                           -- NULL = did not finish
        instances_ready INTEGER,
        tabs_considered INTEGER,
        actions_count INTEGER,
        error TEXT
    )
    """,
    "CREATE INDEX passes_started ON passes(started_at)",
    # js_audit
    """
    CREATE TABLE js_audit (                   -- separate retention: the only trace
        id INTEGER PRIMARY KEY AUTOINCREMENT, -- of arbitrary code execution
        ts INTEGER NOT NULL,
        instance_id TEXT NOT NULL,
        tab_id INTEGER,
        url_at_exec TEXT,
        world TEXT,
        code TEXT NOT NULL,                   -- full, never truncated
        outcome TEXT,                         -- ok | error | disabled
        initiator TEXT NOT NULL,              -- user | mcp | curator
        auth_ctx TEXT,                        -- transport, address, MCP session id
        detail TEXT
    )
    """,
    # quarantine
    """
    CREATE TABLE quarantine (
        instance_id TEXT NOT NULL,
        url TEXT NOT NULL,                    -- keyed by URL, not tab_id: tab_id
        strikes INTEGER NOT NULL DEFAULT 0,   -- evaporates on browser restart
        until INTEGER NOT NULL,
        reason TEXT,
        PRIMARY KEY (instance_id, url)
    )
    """,
    # exemptions
    """
    CREATE TABLE exemptions (
        instance_id TEXT NOT NULL,
        url TEXT NOT NULL,
        until INTEGER NOT NULL,
        reason TEXT,
        PRIMARY KEY (instance_id, url)
    )
    """,
    # quick_links
    """
    CREATE TABLE quick_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        title TEXT,
        position INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    # settings — runtime state, NOT config: pass lease, curator_stopped_at,
    # resume_pending, the runtime execute_js switch.
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)",
]

# --- Version 2: enrollment (§1 of issue #35) --------------------------------
# Enrollment replaces the former shared /ext token: an instance says hello with an
# install_uuid + a per-install secret, lands in enroll_requests, and an operator
# approves it into instances during a short enrollment window. This step only adds
# the STORAGE (tables + columns + the guarding index) and migrates existing rows —
# the /ext handshake, /admin endpoints and revocation are later tasks.
#
# Each ALTER TABLE / CREATE / UPDATE is ONE statement and its OWN list entry: the
# runner executes them individually inside a single BEGIN IMMEDIATE transaction, so
# a SQLite build that rejects multiple ADD COLUMN per statement is never a factor.
_V2_STATEMENTS: list[str] = [
    # Pending enrollment requests. Keyed by install_uuid so a repeat hello UPSERTs
    # the same row rather than piling up.
    """
    CREATE TABLE enroll_requests (
        install_uuid TEXT PRIMARY KEY,
        origin TEXT,                          -- chrome-extension:// origin of the hello
        suggested_title TEXT,                 -- human-readable name proposed by the client
        protocol_version INTEGER NOT NULL,
        secret_hash TEXT NOT NULL,            -- sha256 of the raw secret (server-hashed on
                                              -- receipt); the raw secret is never stored
        first_seen_at INTEGER NOT NULL,       -- NOT bumped by a repeat: else the TTL is never
                                              -- reached and a stale request lives forever
        last_seen_at INTEGER NOT NULL
    )
    """,
    # Audit trail of operator/admin actions (approve, revoke, open-window, ...).
    # Deliberately OUTSIDE retention (src/db/retention.py): a security trail is kept.
    """
    CREATE TABLE admin_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        action TEXT NOT NULL,
        install_uuid TEXT,
        instance_id TEXT,
        initiator TEXT NOT NULL,              -- who acted: admin | system
        detail TEXT
    )
    """,
    "CREATE INDEX admin_audit_ts ON admin_audit(ts)",
    # New instances columns. Default 'pending' (NOT 'active'): a post-migration hello
    # must not silently re-activate a row — approval is an explicit later step.
    "ALTER TABLE instances ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE instances ADD COLUMN secret_hash TEXT",
    "ALTER TABLE instances ADD COLUMN install_uuid TEXT",
    "ALTER TABLE instances ADD COLUMN enrolled_at INTEGER",
    "ALTER TABLE instances ADD COLUMN revoked_at INTEGER",
    # One enrolled secret per instance. NULL values do NOT collide under a UNIQUE
    # index in SQLite, so every not-yet-enrolled row (secret_hash IS NULL) coexists.
    "CREATE UNIQUE INDEX instances_secret_hash ON instances(secret_hash)",
    # Migrate EVERY pre-existing instance (including MAIN) to 'revoked': before
    # enrollment there were no secrets, so none of these rows is enrolled. After the
    # upgrade MAIN therefore requires explicit re-approval — a documented consequence
    # (a later task raises the operator alert).
    "UPDATE instances SET status='revoked' WHERE secret_hash IS NULL",
]

# --- Version 3: enrollment without approval (§6) -----------------------------
# The two-step enrollment is gone. The WINDOW is the permission: an enroll_request that
# carries the right code into an open window creates the ACTIVE instance immediately,
# under the id the human typed in the extension settings. So the whole pending-request
# storage goes with it, and with it the client-proposed name:
#
# * ``enroll_requests`` — nothing writes or reads it anymore. The channel enrolls straight
#   into ``instances``; there is no operator list to hold, no TTL to sweep, no capacity
#   to cap. Dropping the TABLE (rather than leaving it orphaned) is what makes the
#   simplification real: a leftover table is an invitation to wire the second step back.
# * ``instances.title`` — the browser proposed a display name AND the operator separately
#   assigned an id, with no rename anywhere in the product to justify the split. One name
#   per system now: the id IS the name, and every surface that printed ``title`` prints
#   the id.
#
# ``ALTER TABLE ... DROP COLUMN`` needs SQLite >= 3.35 (measured: 3.46.1 in the shipping
# python:3.11-slim image, 3.53.3 on the dev machine) and refuses a column that is indexed
# or referenced by a view/trigger — ``title`` is plain, so this is a single statement
# rather than the twelve-column table rebuild the older recipe would need.
_V3_STATEMENTS: list[str] = [
    "DROP TABLE enroll_requests",
    "ALTER TABLE instances DROP COLUMN title",
]

# --- Version 4: the capability report (§11/§12) ------------------------------
# ``allow_execute_js`` has been reported in ``hello`` since v1, but it was the ONLY thing
# an instance said about itself. An agent could not find out what a copy allows until a
# verb failed mid-task, which is the expensive moment to learn it. Two more facts ride the
# same hello -> instances -> list_instances path:
#
# * ``allow_debugger`` — the second per-copy options checkbox, default OFF like its
#   sibling. NO verb reads it as a gate yet; it is the switch a later screenshot/CDP path
#   will read. It lands NOW because the report is the point: a capability that appears
#   only with its first consumer forces every agent written before that day to discover
#   the answer by failing. DEFAULT 0 — a column that defaulted to 1 would report every
#   pre-migration instance as debugger-capable, which is the opposite of the truth.
# * ``ext_version`` — which extension bundle is actually running (from
#   ``chrome.runtime.getManifest().version``). The service and the extension update by
#   DIFFERENT paths (the Dockerfile does not ship ``extension/``), so "new service + old
#   extension" is a guaranteed state; without this nothing could tell an agent that the
#   copy it is talking to predates a verb. NULLable: an instance that has not said hello
#   since the upgrade genuinely has not told us, and "unknown" must not be spelled as a
#   made-up version string.
#
# Each ALTER TABLE is its own statement (the runner executes them in order inside one
# BEGIN IMMEDIATE), matching the v2 style.
_V4_STATEMENTS: list[str] = [
    "ALTER TABLE instances ADD COLUMN allow_debugger INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE instances ADD COLUMN ext_version TEXT",
]

# --- Version 5: HOW the audited code was executed (§12) ----------------------
# ``js_audit.code`` records WHAT ran; ``await_promise`` records the mode it ran IN, and
# without it the stored text is ambiguous. The same source means two different things on
# the two paths: through indirect eval it runs in the page's global scope and a top-level
# `return` is a SyntaxError, while as an async-function body it has its own scope and
# `return` is how it answers. §12 justifies keeping the code in full because "усечённый код
# нереконструируем" — a row that cannot say which of the two produced its effect is
# unreconstructable for the same reason, one field over.
#
# DEFAULT 0 is the honest backfill, not a convenience: every row written before this column
# existed came from the eval path, because that was the only path.
_V5_STATEMENTS: list[str] = [
    "ALTER TABLE js_audit ADD COLUMN await_promise INTEGER NOT NULL DEFAULT 0",
]

# --- Version 6: the single JS & Debugger checkbox (§12) ----------------------
# ``allow_debugger`` (added in v4) was the mirror of a SECOND per-copy options checkbox that
# was meant to gate a future chrome.debugger path. That path arrives (wave 18, starting with
# ``set_focus_emulation``), but the owner's decision is ONE checkbox for both surfaces: the
# live ``allow_execute_js`` gate now covers execute_js AND the debugger path. That makes a
# separate ``allow_debugger`` meaningless — nothing reads it, and keeping it would report a
# capability the product no longer has. So the column goes.
#
# The gate KEY on the extension side keeps its historical name ``allowExecuteJs`` on purpose
# (renaming it would reset every installed copy's checkbox to OFF); only the dead mirror
# column is dropped here.
#
# ``ALTER TABLE ... DROP COLUMN`` needs SQLite >= 3.35 (measured: 3.46.1 in the shipping
# python:3.11-slim image, 3.53.3 on the dev machine) and refuses a column that is indexed or
# referenced by a view/trigger — ``allow_debugger`` is a plain, unindexed column (like
# ``title`` in v3), so from the migration's side this is a single ALTER statement, not a
# hand-rolled table rebuild — even though SQLite may internally rewrite the table to run it.
_V6_STATEMENTS: list[str] = [
    "ALTER TABLE instances DROP COLUMN allow_debugger",
]

# Ordered list of steps. Append new steps with the next target_version and bump
# MAX_VERSION; never edit a shipped step (a migrated DB has already run it).
STEPS: list[tuple[int, list[str]]] = [
    (1, _V1_STATEMENTS),
    (2, _V2_STATEMENTS),
    (3, _V3_STATEMENTS),
    (4, _V4_STATEMENTS),
    (5, _V5_STATEMENTS),
    (6, _V6_STATEMENTS),
]

MAX_VERSION: int = max(target for target, _ in STEPS)
