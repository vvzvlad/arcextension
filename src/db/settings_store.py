"""Synchronous ``fn(conn)`` bodies for the runtime ``settings`` table.

The ``settings`` table holds RUNTIME state, not config (§4/§12): unlike ENV knobs
in :mod:`src.settings`, these rows are mutable while the service runs. This phase
adds one row — the ``execute_js`` runtime kill-switch (§12 "запретить execute_js
везде сейчас") — the single "stop" button for the most dangerous operation in the
system.

Each function runs inside one ``Database.write`` / ``Database.read`` call (no
``await`` inside; Фаза 2 contract). Values are stored as text.
"""

from __future__ import annotations

import sqlite3

# The kill-switch row key. ABSENT row => default ENABLED (the switch has never
# been flipped, so execute_js works). Only an explicit "off" value disables it —
# so a fresh DB, or a value we do not recognise, never silently kills execute_js.
EXECUTE_JS_ENABLED_KEY = "execute_js_enabled"

# Values that mean "disabled". Everything else (including an absent row) is
# treated as enabled.
_FALSE_VALUES = frozenset({"0", "false", "off", "no"})


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


def is_execute_js_enabled(conn: sqlite3.Connection) -> bool:
    """Whether execute_js is allowed by the runtime kill-switch.

    Default ENABLED: an absent row (never flipped) returns ``True``. Returns
    ``False`` only when the row explicitly holds a false value, so an unreadable /
    unexpected value fails OPEN rather than silently blocking curation — the
    switch is an explicit owner action, not an accident.
    """
    value = get_setting(conn, EXECUTE_JS_ENABLED_KEY)
    if value is None:
        return True
    return value.strip().lower() not in _FALSE_VALUES


def set_execute_js_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    """Flip the runtime execute_js kill-switch on/off."""
    set_setting(conn, EXECUTE_JS_ENABLED_KEY, "1" if enabled else "0")
