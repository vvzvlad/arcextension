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
ALLOWED_STATUSES = frozenset({"done", "failed", "deferred", "abandoned"})

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
