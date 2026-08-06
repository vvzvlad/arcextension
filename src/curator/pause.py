"""Stop / start the curator — the settings write + the fencing-epoch bump (§7/§12).

The pass reads ``curator_stopped_at`` at step 1 (:mod:`src.curator.runner`) and returns
``{"status": "stopped"}`` while it is set; ``bump_epoch`` additionally STOPS a pass
that is already in flight (its next lease-guarded write then changes zero rows). These
synchronous ``fn(conn)`` bodies are the single, reusable stop / start primitives — the
WRITE shape lives here ONCE and both callers converge on it:

* the MCP ``pause`` / ``resume`` tools (Фаза 11), and
* the HTTP ``POST/DELETE /api/pause`` endpoints (Фаза 16).

Each runs inside one ``Database.write`` transaction (Фаза 2 contract: no ``await``
inside; one BEGIN/COMMIT). ``curator_stopped_at`` is stored as text ms, exactly the
shape ``runner.get_setting_int`` decodes; clearing it writes an empty string, which
that decoder reads back as "not set".

Two ``settings`` rows model the state (§7): ``curator_stopped_at`` (the moment the
stop was pressed; absent/blank = running — the stop is INDEFINITE, there is no
deadline and no expiry) and ``resume_pending`` (the over-threshold "waiting for a
click" latch, owned by the runner — see the threshold gate in
:mod:`src.curator.runner`). The start applies the TTL shift: every ``exemptions.until``
/ ``quarantine.until`` that outlived ``curator_stopped_at`` moves forward by the ACTUAL
stop duration, so a protection issued before a long stop is not silently consumed by
the stopped hours (§7 "Старт сдвигает TTL-защиты").
"""

from __future__ import annotations

import sqlite3

from src.curator import lease
from src.db.settings_store import get_setting, set_setting

# The runtime settings keys. ``STOPPED_AT_KEY`` is read by the pass at step 1, by the
# /api guards, by /metrics and by /api/state — the rows MUST share a name, so everybody
# imports it from here. ``RESUME_PENDING_KEY`` is the runner's threshold latch.
STOPPED_AT_KEY = "curator_stopped_at"
RESUME_PENDING_KEY = "resume_pending"


def _to_int(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def stop(conn: sqlite3.Connection, *, now: int) -> int:
    """Stop the curator indefinitely and fence any in-flight pass. Returns stopped_at.

    Writes ``curator_stopped_at`` (so the NEXT pass short-circuits at step 1) AND bumps
    the fencing epoch (so a pass running RIGHT NOW loses its lease on its next guarded
    write — §7). IDEMPOTENT on the timestamp: a re-press while already stopped keeps
    the ORIGINAL stop time, because the start-side TTL shift must reflect the FULL
    stop duration, not the interval since the last nervous re-press.

    The lease SLOT is deliberately NOT freed here. The fenced pass stops at its next
    guarded write and then releases the slot itself (:func:`src.curator.lease.release`
    is keyed on the OWNER, so a moved epoch no longer blocks it) — which is what keeps
    the §7 promise of an immediate pass after a hand-lifted stop without ever letting
    two passes overlap. Freeing the slot here would: ``run_phase_a`` and
    ``run_window_merge`` send their browser command BEFORE their first guarded write, so
    a pass triggered by a start seconds later could begin while the fenced one is still
    inside ``send_command``.
    """
    existing = _to_int(get_setting(conn, STOPPED_AT_KEY))
    stopped_at = existing if existing is not None else now
    if existing is None:
        set_setting(conn, STOPPED_AT_KEY, str(now))
    # Bump the epoch so an already-running pass is fenced out immediately, not only
    # the next one blocked by the stop. The slot stays held until that pass actually
    # finishes and releases it (see the docstring).
    lease.bump_epoch(conn)
    return stopped_at


def apply_resume_shift(conn: sqlite3.Connection, *, now: int) -> int:
    """Start the curator: shift the TTL protections by the ACTUAL stop duration and
    clear ``curator_stopped_at`` — EXACTLY ONCE per stop.

    This is THE single place the shift is computed and applied (§7). The shift is
    ``max(0, now - stopped_at)`` — the real elapsed stopped time, however long the
    owner left the curator off. Every ``exemptions.until`` / ``quarantine.until`` that
    outlived ``stopped_at`` moves forward by the shift; protections that had already
    expired before the stop are left alone (§7). Idempotency is structural: the
    function CLEARS ``curator_stopped_at``, so a second call finds no armed stop and
    shifts nothing — a guarded no-op when the curator is already running.

    ``resume_pending`` is deliberately left untouched (the runner owns that latch):
    the pass the start triggers has to still SEE it. A latch-confirm click consumes
    it, and a start's normal-gated pass either executes an under-threshold plan
    (clearing the latch itself) or refreshes it for the informed second click — see
    :func:`src.api.pause.resume_now` for the two meanings of the one verb. Returns
    the shift (ms).
    """
    started = _to_int(get_setting(conn, STOPPED_AT_KEY))
    if started is None:
        # Not stopped (never stopped, or the start already ran) — nothing to do.
        return 0
    shift = max(0, now - started)
    if shift > 0:
        # Parameterized UPDATEs (Фаза 2). Only protections still in the future relative
        # to the stop START are moved — an already-expired one stays expired.
        conn.execute(
            "UPDATE exemptions SET until = until + ? WHERE until > ?", (shift, started)
        )
        conn.execute(
            "UPDATE quarantine SET until = until + ? WHERE until > ?", (shift, started)
        )
    # Clear the stop (the once-guard): the curator is running again.
    set_setting(conn, STOPPED_AT_KEY, "")
    return shift


def read_stopped_at(conn: sqlite3.Connection) -> int | None:
    """Return ``curator_stopped_at`` (ms) or ``None`` when the curator is running.

    Reused by the MCP mutating-verb guard, the HTTP stop gate (Фаза 16), the
    ``/api/state`` assembler and ``list_instances``. An absent/blank row => None.
    """
    return _to_int(get_setting(conn, STOPPED_AT_KEY))
