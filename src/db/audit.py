"""Synchronous ``fn(conn)`` bodies for the ``js_audit`` table (§12).

``js_audit`` is the ONLY durable trace of arbitrary code execution, so a row is
written BEFORE an ``execute_js`` command is sent — a disabled, rejected or
timed-out call must be recorded too — and its ``outcome`` is updated once known.
The code is stored in full, never truncated (a truncated payload is
unreconstructable). This table has its OWN, longer retention (see
``src/db/retention.py``): sweeping it with routine ``dedupe_close`` noise would
destroy the only evidence of code execution.

Every function runs inside one ``Database.write`` transaction (no ``await``).
"""

from __future__ import annotations

import sqlite3

_INSERT_JS_AUDIT = """
INSERT INTO js_audit (ts, instance_id, tab_id, url_at_exec, world, code,
                      outcome, initiator, auth_ctx, detail)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def insert_js_audit(
    conn: sqlite3.Connection,
    *,
    instance_id: str,
    code: str,
    initiator: str,
    now: int,
    tab_id: int | None = None,
    url_at_exec: str | None = None,
    world: str | None = None,
    auth_ctx: str | None = None,
    outcome: str | None = None,
    detail: str | None = None,
) -> int:
    """Record an ``execute_js`` attempt BEFORE it is sent; return the row id.

    ``outcome`` starts NULL (unknown) and is filled by
    :func:`update_js_audit_outcome` once the response — or a timeout — is known.
    """
    cur = conn.execute(
        _INSERT_JS_AUDIT,
        (
            now,
            instance_id,
            tab_id,
            url_at_exec,
            world,
            code,  # full, never truncated (§12)
            outcome,
            initiator,
            auth_ctx,
            detail,
        ),
    )
    return int(cur.lastrowid)


def update_js_audit_outcome(
    conn: sqlite3.Connection,
    audit_id: int,
    outcome: str,
    detail: str | None = None,
) -> None:
    """Set ``outcome`` (``ok`` | ``error`` | ``disabled``) on an audit row.

    ``detail`` is only overwritten when a non-None value is supplied, so a
    success does not erase context recorded at insert time.
    """
    if detail is None:
        conn.execute(
            "UPDATE js_audit SET outcome = ? WHERE id = ?", (outcome, audit_id)
        )
    else:
        conn.execute(
            "UPDATE js_audit SET outcome = ?, detail = ? WHERE id = ?",
            (outcome, detail, audit_id),
        )
