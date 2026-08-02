"""Ordered migration steps. The schema here is the canon copied verbatim from
docs/architecture.md §4 (lines 129-270) — column names, types, defaults and
primary keys must match §4 exactly. The migration runner (migrations.py) applies
these steps under an explicit BEGIN IMMEDIATE transaction each.

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
    # settings — runtime state, NOT config: pass lease, pause_until,
    # pause_started_at, resume_pending, the runtime execute_js switch.
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)",
]

# Ordered list of steps. Append new steps with the next target_version and bump
# MAX_VERSION; never edit a shipped step (a migrated DB has already run it).
STEPS: list[tuple[int, list[str]]] = [
    (1, _V1_STATEMENTS),
]

MAX_VERSION: int = max(target for target, _ in STEPS)
