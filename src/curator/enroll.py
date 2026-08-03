"""Enrollment window — arm / read / close, stored in the ``settings`` TABLE (§13).

Enrollment replaces the former shared /ext token: an instance says hello with an
``install_uuid`` + a per-install secret and waits in ``enroll_requests`` until an
operator approves it. Approval is only accepted while the enrollment WINDOW is open —
a short, operator-opened interval so a stolen hello cannot be approved at an arbitrary
later time.

The window is modelled exactly like ``pause_until`` in :mod:`src.curator.pause`: a
deadline stored in the ``settings`` key/value table and compared against ``now`` AT
READ TIME, NOT a background timer. That is deliberate — a timer would either silently
close the window on a restart (an operator mid-approval loses it) or, if re-armed on
boot, leave it open forever. Reading the deadline and comparing it to the caller's
``now`` makes a restart a no-op: the window stays open until its stored deadline and
then reads closed, with no thread to keep alive.

Two ``settings`` rows model an open window:

* ``enroll_window_until`` — the absolute deadline (epoch ms, the SAME unit
  ``pause_until`` uses), and
* ``enroll_window_code`` — a short, human-typeable code REGENERATED on every arm
  (issue #35: "код новый на каждое открытие окна"), so a code leaked from a previous
  window cannot be used to approve during a later one.

Each ``fn(conn)`` body runs inside one ``Database.write`` / ``Database.read``
transaction (Фаза 2 contract: no ``await`` inside; one BEGIN/COMMIT). Values are
stored as text, exactly the shape :func:`src.db.settings_store.get_setting` returns.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

from src.db.settings_store import get_setting, set_setting

# The runtime settings keys for the enrollment window. Kept here as the single source
# of the row names so every reader/writer converges on the same rows.
ENROLL_WINDOW_UNTIL_KEY = "enroll_window_until"
ENROLL_WINDOW_CODE_KEY = "enroll_window_code"

# Code alphabet: unambiguous, human-typeable — no 0/O or 1/I/L that get misread when an
# operator reads the code off one screen and types it on another. Uppercase only for the
# same reason. Six characters over 31 symbols is ~29.7 bits: plenty for a short-lived,
# single-window code that is ALSO gated by the window being open at read time.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6

# The enrollment window is a SHORT operator-opened interval (docstring above): its whole
# security value is that a stolen hello cannot be approved at an arbitrary later time. An
# unbounded ENROLL_WINDOW_MIN (e.g. 525600 = a year) would make the window effectively
# always-open and void that guarantee, so — exactly like pause.PAUSE_MAX_MIN — the armed
# length is clamped to a finite ceiling here, the single write-shape for the window.
ENROLL_WINDOW_MAX_MIN = 60


def _to_int(raw) -> int | None:
    """Decode a settings text value to int; absent/blank/garbage => ``None``.

    Mirrors :func:`src.curator.pause._to_int` so an absent or cleared row reads back
    as "no window", never as a spurious ``0`` deadline.
    """
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _new_code() -> str:
    """Generate a fresh, short, human-typeable window code."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


@dataclass(frozen=True)
class EnrollWindowState:
    """Read-time snapshot of the enrollment window.

    ``open`` is the read-time comparison result; ``seconds_remaining`` is 0 whenever the
    window is closed (absent deadline OR already past ``now``). ``code`` / ``until`` carry
    the stored values ONLY while ``open`` is True; whenever the window is not open (never
    armed, explicitly closed, or past its deadline) both read ``None`` — so a caller can
    render ``code`` without separately checking ``open`` and never surface a dead code.
    """

    open: bool
    seconds_remaining: int
    code: str | None
    until: int | None


def arm_enroll_window(conn: sqlite3.Connection, *, now: int, minutes: int) -> EnrollWindowState:
    """Open (or re-open) the enrollment window until ``now + minutes`` and mint a code.

    Writes ``enroll_window_until = now + minutes*60_000`` and a FRESH
    ``enroll_window_code`` (a new code on every open — a previous window's code never
    carries over). Returns the resulting :class:`EnrollWindowState` (open, with the new
    code and full duration remaining). ``minutes`` is the caller-resolved window length
    (``settings.enroll_window_min`` by default); a non-positive value would arm an
    already-closed window, so it is floored to 1 minute here as a last line of defence.
    """
    if minutes < 1:
        minutes = 1
    elif minutes > ENROLL_WINDOW_MAX_MIN:
        # An absurdly large window would void the short-window guarantee (docstring).
        minutes = ENROLL_WINDOW_MAX_MIN
    until = now + minutes * 60_000
    code = _new_code()
    set_setting(conn, ENROLL_WINDOW_UNTIL_KEY, str(until))
    set_setting(conn, ENROLL_WINDOW_CODE_KEY, code)
    remaining_ms = until - now
    return EnrollWindowState(
        open=True,
        seconds_remaining=_ceil_seconds(remaining_ms),
        code=code,
        until=until,
    )


def read_enroll_window(conn: sqlite3.Connection, *, now: int) -> EnrollWindowState:
    """Return the window state by comparing the stored deadline against ``now``.

    This is the read-time compare (no timer): the window is OPEN iff a deadline is
    armed AND still in the future relative to the caller's ``now``. An absent deadline,
    or one at/behind ``now``, reads as closed with 0 seconds remaining. Because the only
    input is the stored row + ``now``, a process restart changes nothing — the same
    deadline still reads open until it is genuinely past.
    """
    until = _to_int(get_setting(conn, ENROLL_WINDOW_UNTIL_KEY))
    code = get_setting(conn, ENROLL_WINDOW_CODE_KEY) or None
    if until is None:
        # No (or unparseable) deadline: closed. code=None, NOT the stored code — the
        # docstring's contract is that a caller may render ``state.code`` without checking
        # ``state.open``, and the two closed branches must therefore be symmetric. Returning
        # the row here handed a live-looking code back whenever the deadline row was
        # missing/blank/garbage while the code row survived — the exact shape a half-written
        # or hand-edited settings pair leaves behind.
        return EnrollWindowState(open=False, seconds_remaining=0, code=None, until=None)
    remaining_ms = until - now
    if remaining_ms <= 0:
        # Deadline reached: closed at read time, no seconds left. The stored rows are left
        # in place (a stale-but-past deadline is harmless and reads closed) until the next
        # arm overwrites them or an explicit close clears them — but the state we RETURN
        # reports code/until = None, symmetric with the absent-deadline branch, so a caller
        # that renders state.code without checking state.open can never show a dead code.
        return EnrollWindowState(open=False, seconds_remaining=0, code=None, until=None)
    return EnrollWindowState(
        open=True,
        seconds_remaining=_ceil_seconds(remaining_ms),
        code=code,
        until=until,
    )


def close_enroll_window(conn: sqlite3.Connection) -> None:
    """Close the window NOW: clear both the deadline and the code.

    Writes empty strings (which ``get_setting`` / ``_to_int`` read back as "not set"),
    matching how :mod:`src.curator.pause` clears ``pause_until``. After this a read
    returns a closed state with no code.
    """
    set_setting(conn, ENROLL_WINDOW_UNTIL_KEY, "")
    set_setting(conn, ENROLL_WINDOW_CODE_KEY, "")


def _ceil_seconds(ms: int) -> int:
    """Whole seconds remaining, rounded UP so a still-open sub-second window reports >=1."""
    if ms <= 0:
        return 0
    return (ms + 999) // 1000
