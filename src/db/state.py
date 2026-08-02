"""Read the ``StateResponse`` mirror for ``GET /api/state`` (§10).

Pure synchronous ``fn(conn)`` bodies runnable from ``Database.read``. The endpoint
returns this straight from the mirror IMMEDIATELY and (separately) kicks a
background single-flight snapshot refresh — this module never does I/O or awaits
(Фаза 2 contract). Every SELECT is static and parameter-free; the shape mirrors
§10's ``StateResponse`` exactly.
"""

from __future__ import annotations

import sqlite3

# Column lists kept next to their SELECTs so the JSON shape and the SQL never drift
# from §10's StateResponse.
_INSTANCE_COLUMNS = (
    "id",
    "title",
    "connected",
    "snapshot_at",
    "last_seen_at",
    "reject_reason",
    "reject_at",
    "focused_window_id",
)
_TAB_COLUMNS = (
    "instance_id",
    "tab_id",
    "window_id",
    "url",
    "title",
    "fav_icon_url",
    "pinned",
    "active",
    "audible",
    "last_active_at",
    "age_unknown",
)


def _read_instances(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT " + ", ".join(_INSTANCE_COLUMNS) + " FROM instances ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "connected": bool(r["connected"]),
            "snapshot_at": r["snapshot_at"],
            "last_seen_at": r["last_seen_at"],
            "reject_reason": r["reject_reason"],
            "reject_at": r["reject_at"],
            "focused_window_id": r["focused_window_id"],
        }
        for r in rows
    ]


def _read_tabs(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT " + ", ".join(_TAB_COLUMNS) + " FROM tabs "
        "ORDER BY instance_id, tab_id"
    ).fetchall()
    return [
        {
            "instance_id": r["instance_id"],
            "tab_id": r["tab_id"],
            "window_id": r["window_id"],
            "url": r["url"],
            "title": r["title"],
            "fav_icon_url": r["fav_icon_url"],
            "pinned": bool(r["pinned"]),
            "active": bool(r["active"]),
            "audible": bool(r["audible"]),
            "last_active_at": r["last_active_at"],
            "age_unknown": bool(r["age_unknown"]),
        }
        for r in rows
    ]


def _read_quick_links(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, url, title, position FROM quick_links ORDER BY position, id"
    ).fetchall()
    return [
        {"id": r["id"], "url": r["url"], "title": r["title"], "position": r["position"]}
        for r in rows
    ]


def _read_last_pass(conn: sqlite3.Connection) -> tuple[int | None, bool | None]:
    """``(last_pass_at, last_pass_ok)`` from the ``passes`` table (§12: pass facts
    come from ``passes``, never from process memory or ``max(actions.ts)``).

    The most recent pass by ``started_at``; ``last_pass_at`` prefers ``finished_at``
    (a finished pass) and falls back to ``started_at`` (one still running / killed).
    ``last_pass_ok`` is NULL until the pass finished.
    """
    row = conn.execute(
        "SELECT started_at, finished_at, ok FROM passes "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return (None, None)
    started_at, finished_at, ok = row[0], row[1], row[2]
    last_at = finished_at if finished_at is not None else started_at
    last_ok = None if ok is None else bool(ok)
    return (last_at, last_ok)


def _read_rule_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """``(rules_total, rules_invalid)`` — the §10/§12 pair; invalid rules are
    excluded from matching but still counted (an orphaned target is invalid)."""
    total = int(conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0])
    invalid = int(
        conn.execute("SELECT COUNT(*) FROM rules WHERE invalid = 1").fetchone()[0]
    )
    return (total, invalid)


def build_state(conn: sqlite3.Connection, server_now: int) -> dict:
    """Assemble the full ``StateResponse`` (§10) from the mirror in ONE reader
    connection. ``server_now`` is stamped by the caller (server clock)."""
    last_pass_at, last_pass_ok = _read_last_pass(conn)
    rules_total, rules_invalid = _read_rule_counts(conn)
    return {
        "server_now": server_now,
        "last_pass_at": last_pass_at,
        "last_pass_ok": last_pass_ok,
        "rules_total": rules_total,
        "rules_invalid": rules_invalid,
        "instances": _read_instances(conn),
        "tabs": _read_tabs(conn),
        "quick_links": _read_quick_links(conn),
    }


def connected_snapshot_ages(conn: sqlite3.Connection) -> dict[str, int | None]:
    """``{instance_id: snapshot_at}`` for every ``connected=1`` instance — the input
    to the single-flight staleness decision in the endpoint (§10). ``snapshot_at``
    may be NULL (connected, never snapshotted) → always stale."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, snapshot_at FROM instances WHERE connected = 1"
    ).fetchall()
    return {r["id"]: r["snapshot_at"] for r in rows}
