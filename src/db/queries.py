"""Synchronous ``fn(conn)`` bodies for ``Database.write`` used by the /ext channel.

Every function here is a plain synchronous callable that runs INSIDE one
``Database.write`` transaction (Фаза 2 contract: no ``await``, one BEGIN/COMMIT).
They are kept out of ``channel.py`` so the exact SQL of the two load-bearing
invariants — the epoch-guarded disconnect UPDATE and the hello upsert that reads
its own new ``conn_epoch`` back in the same transaction — is in one place and
mutation-testable.
"""

from __future__ import annotations

import sqlite3

# hello success: create-or-bump the instance row and return the NEW conn_epoch.
# A brand-new instance starts at conn_epoch=1 (0 means "never connected"); a
# reconnect does conn_epoch+1. connected=1 and the reject fields are cleared. The
# new epoch is read back in the SAME transaction so the caller can hand it to the
# connection's finalizer (§6).
_HELLO_UPSERT = """
INSERT INTO instances (id, title, conn_epoch, connected, session_id,
                       allow_execute_js, last_seen_at, reject_reason, reject_at)
VALUES (?, ?, 1, 1, ?, ?, ?, NULL, NULL)
ON CONFLICT(id) DO UPDATE SET
    conn_epoch = conn_epoch + 1,
    connected = 1,
    session_id = excluded.session_id,
    title = excluded.title,
    allow_execute_js = excluded.allow_execute_js,
    last_seen_at = excluded.last_seen_at,
    reject_reason = NULL,
    reject_at = NULL
"""


def hello_upsert(
    conn: sqlite3.Connection,
    instance_id: str,
    session_id: str | None,
    title: str | None,
    allow_execute_js: bool,
    now: int,
) -> int:
    """Register a successful hello; return the instance's new ``conn_epoch``."""
    conn.execute(
        _HELLO_UPSERT,
        (instance_id, title, session_id, 1 if allow_execute_js else 0, now),
    )
    row = conn.execute(
        "SELECT conn_epoch FROM instances WHERE id = ?", (instance_id,)
    ).fetchone()
    return int(row[0])


# Record the reason of the most recent rejection. UPSERT because the instanceId
# may never have connected (a brand-new row is created with connected=0); for an
# existing row ONLY the reject fields are touched — connected / conn_epoch /
# focused_window_id are left exactly as they are, so recording a duplicate-instance
# rejection never disturbs the live socket that already owns the id (§6).
_RECORD_REJECTION = """
INSERT INTO instances (id, connected, reject_reason, reject_at)
VALUES (?, 0, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    reject_reason = excluded.reject_reason,
    reject_at = excluded.reject_at
"""


def record_rejection(
    conn: sqlite3.Connection, instance_id: str, reason: str, now: int
) -> None:
    conn.execute(_RECORD_REJECTION, (instance_id, reason, now))


def mark_disconnected(
    conn: sqlite3.Connection, instance_id: str, conn_epoch: int
) -> None:
    """Epoch-guarded disconnect write — THE headline invariant (§6).

    Every disconnect path (clean close, error, heartbeat miss, eviction of the
    old socket) calls this with the epoch THAT socket owned. The
    ``AND conn_epoch = ?`` guard is what makes a half-dead old socket finalized
    30s late a no-op: by then a healthy newer connection has bumped the epoch, so
    this UPDATE matches nothing and does NOT clear the live connection. Dropping
    the guard would strand the instance out of curation forever on a working link.
    """
    conn.execute(
        "UPDATE instances SET connected = 0, focused_window_id = NULL "
        "WHERE id = ? AND conn_epoch = ?",
        (instance_id, conn_epoch),
    )
