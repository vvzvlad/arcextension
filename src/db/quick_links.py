"""Synchronous ``fn(conn)`` bodies for the ``quick_links`` table (§4/§10).

Every mutating function here runs INSIDE one ``Database.write`` transaction (Фаза 2
contract: no ``await``, one BEGIN/COMMIT). All SQL is parameterized; no id or
position is ever string-interpolated.

The startpage keeps an OFFLINE op queue in ``chrome.storage.local`` and flushes it
with ``POST /api/quick_links/ops`` carrying an ``Idempotency-Key`` (§10). The
server is the authority on ``position`` (append) and applies the batch
last-write-wins by receive time; ``reorder`` is the explicit reposition op.

The ``quick_links`` schema (§4) is::

    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE,
    title TEXT, position INTEGER NOT NULL, created_at INTEGER NOT NULL
"""

from __future__ import annotations

import sqlite3

# Idempotency markers live in the runtime ``settings`` table under this prefix. A
# retried flush resends the SAME batch with the SAME key; recording the key lets a
# retry be a no-op instead of double-applying (§10 "Idempotency-Key").
_IDEMPOTENCY_PREFIX = "qlkey:"


def list_quick_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All quick links ordered by ``position`` (the render order, §10)."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, url, title, position FROM quick_links ORDER BY position, id"
    ).fetchall()


def _next_position(conn: sqlite3.Connection) -> int:
    """The append position: one past the current maximum (server-side, §10)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM quick_links"
    ).fetchone()
    return int(row[0]) + 1


def _add(conn: sqlite3.Connection, url: str, title: str | None, now: int) -> None:
    """Append a link. ``position`` is SERVER-side (append) — a client-supplied
    position is ignored (§10). ``url`` is UNIQUE: re-adding an existing url is
    last-write-wins on the title and keeps its current position (no reshuffle)."""
    position = _next_position(conn)
    conn.execute(
        "INSERT INTO quick_links (url, title, position, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET title = excluded.title",
        (url, title, position, now),
    )


def _remove(conn: sqlite3.Connection, link_id, url) -> None:
    """Remove by id when given, else by url (the offline queue may only know the
    url of a link the server assigned no id to yet)."""
    if link_id is not None:
        conn.execute("DELETE FROM quick_links WHERE id = ?", (link_id,))
    elif url is not None:
        conn.execute("DELETE FROM quick_links WHERE url = ?", (url,))


def _reorder(conn: sqlite3.Connection, order) -> None:
    """Explicit reposition (§10): ``order`` is the desired id sequence; positions
    become 0..n-1 in that order. Ids absent from ``quick_links`` are skipped."""
    if not isinstance(order, list):
        return
    for position, link_id in enumerate(order):
        conn.execute(
            "UPDATE quick_links SET position = ? WHERE id = ?", (position, link_id)
        )


def apply_ops(conn: sqlite3.Connection, ops: list[dict], now: int) -> None:
    """Apply a batch of quick-link ops IN ORDER (last-write-wins by arrival, §10).

    Each op is ``{op: 'add'|'remove'|'reorder', ...}``. Unknown ops are ignored
    rather than aborting the whole flush (the offline queue is client-authored and
    must never wedge on a single bad entry).
    """
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "add":
            url = op.get("url")
            if isinstance(url, str) and url:
                _add(conn, url, op.get("title"), now)
        elif kind == "remove":
            _remove(conn, op.get("id"), op.get("url"))
        elif kind == "reorder":
            _reorder(conn, op.get("order"))


def idempotency_key_seen(conn: sqlite3.Connection, key: str) -> bool:
    """Whether this ``Idempotency-Key`` batch was already applied (§10)."""
    row = conn.execute(
        "SELECT 1 FROM settings WHERE key = ?", (_IDEMPOTENCY_PREFIX + key,)
    ).fetchone()
    return row is not None


def record_idempotency_key(conn: sqlite3.Connection, key: str, now: int) -> None:
    """Mark an ``Idempotency-Key`` batch as applied so a retry is a no-op (§10)."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (_IDEMPOTENCY_PREFIX + key, str(now)),
    )


def apply_ops_with_key(
    conn: sqlite3.Connection, ops: list[dict], key: str | None, now: int
) -> list[sqlite3.Row]:
    """One atomic write: apply ``ops`` unless ``key`` was already processed, then
    return the resulting quick-links snapshot. Idempotency and the ops apply in the
    SAME transaction so a retry can never half-apply (§10)."""
    if key is None or not idempotency_key_seen(conn, key):
        apply_ops(conn, ops, now)
        if key is not None:
            record_idempotency_key(conn, key, now)
    return list_quick_links(conn)
