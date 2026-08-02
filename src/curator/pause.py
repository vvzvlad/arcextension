"""Pause / resume the curator — the settings write + the fencing-epoch bump (§7/§12).

The pass reads ``pause_until`` at step 1 (:mod:`src.curator.runner`) and returns
``{"status": "paused"}`` while it is in the future; ``bump_epoch`` additionally
STOPS a pass that is already in flight (its next lease-guarded write then changes
zero rows). These two synchronous ``fn(conn)`` bodies are the single, reusable
"pause" and "resume" primitives:

* Фаза 11 calls them from the MCP ``pause`` / ``resume`` tools.
* Фаза 16 will call the SAME helpers from the HTTP ``/api/pause`` endpoints — the
  write shape must not be duplicated, so both paths converge here.

Each runs inside one ``Database.write`` transaction (Фаза 2 contract: no ``await``
inside; one BEGIN/COMMIT). ``pause_until`` is stored as text ms, exactly the shape
``runner.get_setting_int`` decodes; clearing it writes an empty string, which that
decoder reads back as "not paused".
"""

from __future__ import annotations

import sqlite3

from src.curator import lease
from src.db.settings_store import set_setting

# The runtime settings key the pass reads at step 1. Kept identical to
# ``runner._PAUSE_UNTIL_KEY`` — the two MUST name the same row.
PAUSE_UNTIL_KEY = "pause_until"


def pause(conn: sqlite3.Connection, *, now: int, minutes: int) -> int:
    """Arm a pause until ``now + minutes`` and STOP any in-flight pass.

    Writes ``pause_until`` (so the NEXT pass short-circuits at step 1) AND bumps the
    fencing epoch (so a pass running RIGHT NOW loses its lease on its next guarded
    write — §7). Returns the absolute ``pause_until`` (ms). ``minutes`` is the
    caller's choice, defaulting to ``PAUSE_DEFAULT_MIN`` at the call site.
    """
    until = now + minutes * 60_000
    set_setting(conn, PAUSE_UNTIL_KEY, str(until))
    # Bump the epoch so an already-running pass is fenced out immediately, not only
    # the next one blocked by pause_until.
    lease.bump_epoch(conn)
    return until


def resume(conn: sqlite3.Connection) -> None:
    """Clear the pause (``pause_until`` -> empty = "not paused").

    No epoch bump: resuming only lifts the block; it never needs to stop a pass.
    The ``resume_pending`` continuity-break latch is a SEPARATE mechanism (§7) and
    is deliberately untouched here.
    """
    set_setting(conn, PAUSE_UNTIL_KEY, "")


def read_pause_until(conn: sqlite3.Connection) -> int | None:
    """Return the armed ``pause_until`` (ms) or ``None`` when not paused.

    Reused by the MCP mutating-verb guard (a paused system refuses mutating verbs,
    §12) and available to Фаза 16's status endpoints. An absent/blank row => None.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (PAUSE_UNTIL_KEY,)
    ).fetchone()
    if row is None or row[0] in (None, ""):
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None
