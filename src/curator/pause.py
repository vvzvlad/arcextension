"""Pause / resume the curator — the settings write + the fencing-epoch bump (§7/§12).

The pass reads ``pause_until`` at step 1 (:mod:`src.curator.runner`) and returns
``{"status": "paused"}`` while it is in the future; ``bump_epoch`` additionally
STOPS a pass that is already in flight (its next lease-guarded write then changes
zero rows). These synchronous ``fn(conn)`` bodies are the single, reusable pause /
resume primitives — the WRITE shape lives here ONCE and both callers converge on it:

* Фаза 11 calls them from the MCP ``pause`` / ``resume`` tools.
* Фаза 16 calls the SAME helpers from the HTTP ``POST/DELETE /api/pause`` endpoints.

Each runs inside one ``Database.write`` transaction (Фаза 2 contract: no ``await``
inside; one BEGIN/COMMIT). ``pause_until`` / ``pause_started_at`` are stored as text
ms, exactly the shape ``runner.get_setting_int`` decodes; clearing one writes an empty
string, which that decoder reads back as "not set".

Three ``settings`` rows model a pause (§7): ``pause_until`` (deadline),
``pause_started_at`` (the TRUE start — never rewritten by an extend, so the resume
TTL shift reflects the real duration) and ``resume_pending`` (the after-expiry
"waiting for a click" latch, owned by the runner). The TTL shift moves every
``exemptions.until`` / ``quarantine.until`` that outlived ``pause_started_at`` forward
by the ACTUAL pause duration, so a protection issued before a long pause is not
silently consumed by the paused hour (§7 "Снятие паузы сдвигает TTL-защиты").
"""

from __future__ import annotations

import sqlite3

from src.curator import lease
from src.db.settings_store import get_setting, set_setting

# The runtime settings keys. ``PAUSE_UNTIL_KEY`` is read by the pass at step 1 and by
# /metrics — kept identical to ``runner._PAUSE_UNTIL_KEY`` / ``metrics._PAUSE_UNTIL_KEY``
# (the rows MUST share a name). ``RESUME_PENDING_KEY`` mirrors
# ``runner._RESUME_PENDING_KEY``.
PAUSE_UNTIL_KEY = "pause_until"
PAUSE_STARTED_AT_KEY = "pause_started_at"
RESUME_PENDING_KEY = "resume_pending"

# A pause is ALWAYS finite (§7: "бессрочной не бывает"): an unbounded pause would once
# and for all turn the curator into a dead-and-silent system. Any requested duration is
# clamped to [1, PAUSE_MAX_MIN] here — pause.py is the single write-shape, so both the
# MCP tool and the HTTP endpoint inherit the finiteness guarantee. 24h is a generous
# ceiling (matches the default quarantine horizon) while still guaranteeing self-expiry.
PAUSE_MAX_MIN = 1440


def clamp_minutes(minutes: int | None, default: int) -> int:
    """Clamp a requested pause length to a finite ``[1, PAUSE_MAX_MIN]`` window.

    ``None`` / a non-int / a non-positive value falls back to a sane finite value
    (``default`` when given, else 1); an absurd value is capped at ``PAUSE_MAX_MIN``.
    Callers (endpoint + MCP tool) resolve their own default THEN clamp, so a pause is
    never infinite and never zero-length.
    """
    try:
        m = int(minutes) if minutes is not None else int(default)
    except (TypeError, ValueError):
        # Truly defensive: even a None/garbage default must not raise here (this is the
        # last line before an infinite/zero pause) — fall back to a finite 1 minute.
        try:
            m = int(default)
        except (TypeError, ValueError):
            m = 1
    if m < 1:
        m = 1
    if m > PAUSE_MAX_MIN:
        m = PAUSE_MAX_MIN
    return m


def _to_int(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def pause(conn: sqlite3.Connection, *, now: int, minutes: int) -> int:
    """Arm/extend a pause until ``now + minutes`` and STOP any in-flight pass.

    Writes ``pause_until`` (so the NEXT pass short-circuits at step 1) AND bumps the
    fencing epoch (so a pass running RIGHT NOW loses its lease on its next guarded
    write — §7). ``pause_started_at`` is written ONLY when no pause is currently armed
    (its row absent/blank): a re-press that EXTENDS must keep the ORIGINAL start, so
    the resume TTL shift counts the true elapsed time, not the extended deadline (§7
    "pause_started_at при продлении ... НЕ переписывается"). ``minutes`` is clamped to a
    finite window here as a last line of defence. Returns the absolute ``pause_until``.

    The lease SLOT is deliberately NOT freed here. The fenced pass stops at its next
    guarded write and then releases the slot itself (:func:`src.curator.lease.release`
    is keyed on the OWNER, so a moved epoch no longer blocks it) — which is what keeps
    the §7 promise of an immediate pass after a hand-lifted pause without ever letting
    two passes overlap. Freeing the slot here would: ``run_phase_a`` and
    ``run_window_merge`` send their browser command BEFORE their first guarded write, so
    a pass triggered by a resume seconds later could start while the fenced one is still
    inside ``send_command``.
    """
    minutes = clamp_minutes(minutes, minutes)
    until = now + minutes * 60_000
    set_setting(conn, PAUSE_UNTIL_KEY, str(until))
    # Only stamp the start when this is a NEW pause; an extend keeps the old start.
    if _to_int(get_setting(conn, PAUSE_STARTED_AT_KEY)) is None:
        set_setting(conn, PAUSE_STARTED_AT_KEY, str(now))
    # Bump the epoch so an already-running pass is fenced out immediately, not only
    # the next one blocked by pause_until. The slot stays held until that pass actually
    # finishes and releases it (see the docstring).
    lease.bump_epoch(conn)
    return until


def apply_resume_shift(conn: sqlite3.Connection, *, now: int) -> int:
    """Shift the TTL protections by the ACTUAL pause duration — EXACTLY ONCE per pause.

    This is THE single place the shift is computed and applied (§7). The shift is
    ``max(0, min(now, pause_until) - pause_started_at)``:

    * a manual resume at ``now < pause_until`` shifts by the REAL elapsed time (a pause
      lifted after 5 minutes must not extend a protection by the requested hour), and
    * an expiry-confirm at ``now >= pause_until`` shifts by the FULL pause length.

    Every ``exemptions.until`` / ``quarantine.until`` that outlived ``pause_started_at``
    moves forward by the shift; protections that had already expired before the pause
    are left alone (§7). Idempotency is structural: the function CLEARS
    ``pause_started_at`` (and ``pause_until``), so a second call finds no armed start and
    shifts nothing — this is the double-shift guard that keeps "exactly once" true even
    when the manual-resume pass and a later read both pass through here. ``resume_pending``
    is deliberately left untouched (the runner owns that latch). Returns the shift (ms).
    """
    started = _to_int(get_setting(conn, PAUSE_STARTED_AT_KEY))
    if started is None:
        # No armed pause start (never paused, or the shift already ran) — nothing to do.
        return 0
    until = _to_int(get_setting(conn, PAUSE_UNTIL_KEY))
    end = until if until is not None else now
    shift = max(0, min(now, end) - started)
    if shift > 0:
        # Parameterized UPDATEs (Фаза 2). Only protections still in the future relative
        # to the pause START are moved — an already-expired one stays expired.
        conn.execute(
            "UPDATE exemptions SET until = until + ? WHERE until > ?", (shift, started)
        )
        conn.execute(
            "UPDATE quarantine SET until = until + ? WHERE until > ?", (shift, started)
        )
    # Clear the start (the once-guard) AND the deadline so no stale pause lingers.
    set_setting(conn, PAUSE_STARTED_AT_KEY, "")
    set_setting(conn, PAUSE_UNTIL_KEY, "")
    return shift


def resume(conn: sqlite3.Connection, *, now: int) -> int:
    """Apply the TTL shift once, then clear the pause AND the ``resume_pending`` latch.

    ``apply_resume_shift`` clears ``pause_until`` and ``pause_started_at``; this also
    drops any "waiting for a click" state in one write. Returns the applied shift (ms).

    NOT what the resume BUTTON runs. :func:`src.api.pause.resume_now` composes
    ``apply_resume_shift`` with a CONFIRMING pass instead, precisely because that pass
    must still see the latch: the runner turns ``confirm_pending`` into an ordinary pass
    when nothing is armed, and an ordinary pass after a continuity break walks straight
    back into the gate that armed the latch. The pass clears it (``_finish_continuity``)
    once it has actually run. This whole-state clear is kept for a caller that wants the
    settings write alone, with no pass behind it.
    """
    shift = apply_resume_shift(conn, now=now)
    set_setting(conn, RESUME_PENDING_KEY, "")
    return shift


def read_pause_until(conn: sqlite3.Connection) -> int | None:
    """Return the armed ``pause_until`` (ms) or ``None`` when not paused.

    Reused by the MCP mutating-verb guard, the HTTP pause gate (Фаза 16), the
    ``/api/state`` assembler and ``list_instances``. An absent/blank row => None.
    """
    return _to_int(get_setting(conn, PAUSE_UNTIL_KEY))
