"""Synchronous ``fn(conn)`` bodies for the ``actions`` archive (§4).

Every function here runs INSIDE one ``Database.write`` transaction (Фаза 2
contract: no ``await``, one BEGIN/COMMIT). The ``actions`` row records not only
what happened but the BASIS of the decision — ``rule_id``, ``rule_pattern``
(a deliberate archival COPY, because a rule can be physically deleted) and
``decision`` — so "why was this tab taken" stays answerable after the policy is
edited (§4). All SQL is parameterized; no id is ever string-interpolated.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit, urlunsplit

# Physical operation kinds (§4). ``relocate`` is the phase-A open; the *_close
# kinds are evictions; ``restore`` reopens a taken tab.
#
# ``relocate_close`` is added in Фаза 8: §7 MANDATES a DISTINCT kind for the
# phase-B close ("Отдельный kind обязателен") — while phase B wrote ``dedupe_close``
# the completion of a relocation was indistinguishable from collapsing a duplicate
# (a whole-pass rollback could not tell which row to reverse, and
# ``curator_actions_last_pass{kind}`` summed two different operations). §4's prose
# enum listed only the pre-Фаза-8 kinds; §7 is the canon for the pass and wins.
ALLOWED_KINDS = frozenset(
    {
        "relocate",
        "relocate_close",
        "dedupe_close",
        "singleton_close",
        "window_merge",
        "reset",
        "restore",
    }
)

# Terminal statuses. ``abandoned`` is introduced in Фаза 4: restore of an
# unfinished relocation marks the live ``relocate`` row ``abandoned`` (§10) — a
# legitimate terminal state, distinct from ``failed`` (which means the operation
# was attempted and refused) and ``done``.
#
# ``pending`` is the ONE non-terminal status (Фаза 16, WARNING-1): a ``*_close``
# row written UNDER the lease guard BEFORE the browser ``close_tab``, so that a lease
# lost between a successful close and its completion write does not lose the close's
# record (an audit-journal hole / an un-undoable relocation). The pass's reconcile
# step (runner, EARLY, before decide) resolves every prior-pass ``pending`` against
# the fresh mirror — to ``done`` when the source is gone (the close happened,
# at-least-once journal), or ``abandoned`` when the source is still present (the close
# never took effect, so ``decide`` re-issues it). Readers stay pending-aware:
# ``mirror.live_relocations`` excludes a relocate with a pending relocate_close (phase
# B is in-flight), and ``undo`` skips a pending row (in-flight, nothing to reverse).
ALLOWED_STATUSES = frozenset({"done", "failed", "deferred", "abandoned", "pending"})

# Who initiated the action; orthogonal to ``kind`` (§4).
ALLOWED_INITIATORS = frozenset({"curator", "mcp", "user"})

# Column order for the INSERT below — kept next to the SQL so the two never drift.
_ACTION_COLUMNS = (
    "pass_id",
    "ts",
    "kind",
    "status",
    "instance_from",
    "instance_to",
    "tab_id",
    "session_id_from",
    "tab_id_to",
    "session_id_to",
    "origin_action_id",
    "rule_id",
    "rule_pattern",
    "decision",
    "src_opened_at",
    "src_last_active_at",
    "src_age_unknown",
    "url",
    "url_norm",
    "title",
    "pinned",
    "reason",
    "initiator",
    "detail",
    "restored_at",
)

_INSERT_ACTION = (
    "INSERT INTO actions (" + ", ".join(_ACTION_COLUMNS) + ") "
    "VALUES (" + ", ".join("?" for _ in _ACTION_COLUMNS) + ")"
)


def normalize_url(url: str | None) -> str | None:
    """Return ``origin+path`` of ``url`` — scheme+host(+port)+path, no query or
    fragment. Used for search and dedupe (``url_norm``).

    ``None``/blank in => ``None`` out. A value with no scheme/host is returned
    with query+fragment stripped rather than rejected (best-effort; the archive
    keeps the full ``url`` untouched regardless).
    """
    if not url:
        return None
    parts = urlsplit(url)
    # Drop query (index 3) and fragment (index 4); keep scheme, netloc, path.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def insert_action(
    conn: sqlite3.Connection,
    *,
    ts: int,
    kind: str,
    status: str,
    initiator: str,
    pass_id: str | None = None,
    instance_from: str | None = None,
    instance_to: str | None = None,
    tab_id: int | None = None,
    session_id_from: str | None = None,
    tab_id_to: int | None = None,
    session_id_to: str | None = None,
    origin_action_id: int | None = None,
    rule_id: int | None = None,
    rule_pattern: str | None = None,
    decision: str | None = None,
    src_opened_at: int | None = None,
    src_last_active_at: int | None = None,
    src_age_unknown: int = 0,
    url: str | None = None,
    url_norm: str | None = None,
    title: str | None = None,
    pinned: int | None = None,
    reason: str | None = None,
    detail: str | None = None,
    restored_at: int | None = None,
) -> int:
    """Insert one ``actions`` row covering every §4 column; return its id.

    The three NOT-NULL / enumerated columns (``kind``, ``status``, ``initiator``)
    are validated so a typo lands as a clear error rather than a silently
    unqueryable archive row.
    """
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"invalid actions.kind: {kind!r}")
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid actions.status: {status!r}")
    if initiator not in ALLOWED_INITIATORS:
        raise ValueError(f"invalid actions.initiator: {initiator!r}")

    values = (
        pass_id,
        ts,
        kind,
        status,
        instance_from,
        instance_to,
        tab_id,
        session_id_from,
        tab_id_to,
        session_id_to,
        origin_action_id,
        rule_id,
        rule_pattern,
        decision,
        src_opened_at,
        src_last_active_at,
        1 if src_age_unknown else 0,
        url,
        url_norm,
        title,
        pinned,
        reason,
        initiator,
        detail,
        restored_at,
    )
    cur = conn.execute(_INSERT_ACTION, values)
    return int(cur.lastrowid)


def set_restored_at(conn: sqlite3.Connection, action_id: int, now: int) -> None:
    """Stamp ``restored_at`` on the original action row (idempotency marker for
    undo, §10). Parameterized; never interpolate the id."""
    conn.execute(
        "UPDATE actions SET restored_at = ? WHERE id = ?", (now, action_id)
    )


def mark_action_abandoned(conn: sqlite3.Connection, action_id: int) -> None:
    """Mark an action ``abandoned`` — used by restore to cancel a live but
    unfinished ``relocate`` (§10), so the next pass does not re-close the source."""
    conn.execute(
        "UPDATE actions SET status = 'abandoned' WHERE id = ?", (action_id,)
    )


def set_action_status(
    conn: sqlite3.Connection, action_id: int, status: str, *, reason: str | None = None
) -> None:
    """Update a row's ``status`` in place (Фаза 16, WARNING-1). Used to complete or
    fail a ``pending`` ``*_close`` row once the browser close is known: ``pending`` →
    ``done`` on a successful close, ``pending`` → ``failed`` (with ``reason``) on a
    ``precondition_failed``. Parameterized; never interpolate the id."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid actions.status: {status!r}")
    if reason is not None:
        conn.execute(
            "UPDATE actions SET status = ?, reason = ? WHERE id = ?",
            (status, reason, action_id),
        )
    else:
        conn.execute(
            "UPDATE actions SET status = ? WHERE id = ?", (status, action_id)
        )


def read_pending_closes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Read every ``pending`` close row for the pass's reconcile step (Фаза 16).

    Returns the ``*_close`` rows (relocate_close / dedupe_close / singleton_close)
    left ``pending`` by a prior pass — a close whose browser ``close_tab`` succeeded
    (or is uncertain) but whose completion write was fenced by a lost lease. The
    reconcile step (runner, before decide) resolves each against the fresh mirror.
    Runs before any of THIS pass's own pending writes, so every row it returns is
    necessarily prior-pass and safe to reconcile."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, kind, pass_id, instance_from, instance_to, tab_id, "
        "session_id_from, url, url_norm FROM actions "
        "WHERE status = 'pending' "
        "AND kind IN ('relocate_close', 'dedupe_close', 'singleton_close')"
    ).fetchall()
