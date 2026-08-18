"""Synchronous ``fn(conn)`` bodies for the runtime ``settings`` table.

The ``settings`` table holds RUNTIME state, not config (§4): unlike ENV knobs
in :mod:`src.settings`, these rows are mutable while the service runs. Callers
(e.g. pause/resume) store their own keyed state through the generic helpers
below.

Each function runs inside one ``Database.write`` / ``Database.read`` call (no
``await`` inside; Фаза 2 contract). Values are stored as text.
"""

from __future__ import annotations

import sqlite3


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Return the ``settings.value`` for ``key`` (or ``default`` when absent)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert one ``settings`` row (whole-value last-write-wins)."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
