"""Quarantine strikes and the non-convergence latch (§7 "Карантин" / "Детектор").

The ``quarantine`` table (§4) doubles as BOTH the strike counter and the
suppression, keyed by ``(instance_id, url_normalized)``:

* A strike accrues for a ``precondition_failed`` close AND for a phase-A open that
  keeps repeating without ever completing phase B (§7: "Страйк ставится за
  precondition_failed И за несходимость фазы A"). The runner caps it at one strike
  per pair per pass.
* THREE strikes ⇒ ``until = now + QUARANTINE_TTL_MIN`` and the counter resets; the
  step-4 guard then treats the pair as quarantined until the TTL lapses.
* ANY success on the pair resets the counter (§7). A completed phase B resets it, so
  a healthy relocation — phase A one pass, phase B the next — never reaches three.

The non-convergence detector IS this counter, not a separate status: as a quarantine
row it gets the TTL, the url-change reset, the step-4 guard and the metric for free,
and — being a LATCH keyed by the SOURCE, not a sliding window keyed by the target —
it does not erase its own premise (a window would suppress the third open, drop the
ban, and re-open forever without ever quarantining).

Every function is a synchronous ``fn(conn)`` (Фаза 2 contract). ``url_norm`` is the
key: the step-4 guard reads quarantine by the tab's CURRENT normalized url, so the
strike must be written on the same normalized key (§7).
"""

from __future__ import annotations

import sqlite3

STRIKE_LIMIT = 3  # three consecutive failures/unproductive opens => quarantine (§7)


def _read(conn: sqlite3.Connection, instance_id: str, url_norm: str):
    return conn.execute(
        "SELECT strikes, until, reason FROM quarantine "
        "WHERE instance_id = ? AND url = ?",
        (instance_id, url_norm),
    ).fetchone()


def add_strike(
    conn: sqlite3.Connection,
    instance_id: str,
    url_norm: str,
    *,
    now: int,
    ttl_ms: int,
    reason: str,
) -> bool:
    """Add one strike to ``(instance_id, url_norm)``. Returns True iff this strike
    tripped the quarantine (``until`` set into the future). Reaching the limit resets
    the counter to 0 and stamps ``until``/``reason`` (§7)."""
    row = _read(conn, instance_id, url_norm)
    prev_strikes = int(row[0]) if row is not None else 0
    prev_until = int(row[1]) if row is not None else 0
    prev_reason = row[2] if row is not None else None
    new_strikes = prev_strikes + 1
    if new_strikes >= STRIKE_LIMIT:
        _upsert(conn, instance_id, url_norm, strikes=0, until=now + ttl_ms, reason=reason)
        return True
    # Not tripped yet: keep any existing quarantine window/reason untouched.
    _upsert(conn, instance_id, url_norm, strikes=new_strikes, until=prev_until, reason=prev_reason)
    return False


def reset_strikes(conn: sqlite3.Connection, instance_id: str, url_norm: str) -> None:
    """Zero the strike counter on a success for the pair (§7). Leaves any active
    quarantine ``until`` alone — a success does not lift a still-live quarantine, it
    only clears the accruing counter."""
    if _read(conn, instance_id, url_norm) is not None:
        conn.execute(
            "UPDATE quarantine SET strikes = 0 WHERE instance_id = ? AND url = ?",
            (instance_id, url_norm),
        )


def _upsert(conn, instance_id, url_norm, *, strikes, until, reason) -> None:
    conn.execute(
        "INSERT INTO quarantine (instance_id, url, strikes, until, reason) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(instance_id, url) DO UPDATE SET "
        "strikes = excluded.strikes, until = excluded.until, reason = excluded.reason",
        (instance_id, url_norm, strikes, until, reason),
    )
