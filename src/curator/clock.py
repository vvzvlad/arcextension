"""Server-clock guard + continuity-break fingerprint (§7).

**Clock guard.** Every stored timestamp is absolute server-clock ms; nobody watches
the server's own clock. A forward jump (VM resumed from suspend, an NTP step, a
container that started before time sync) pushes EVERY tab of EVERY instance past
the idle threshold at once — guards protect at most one tab per instance
(``focused_window_id``), and the absolute ``until`` of ``exemptions`` / ``quarantine``
is jumped by the same step. With no action cap (§7) one pass would then evict the
whole fleet. A backward jump makes ``serverNow - last_active_at`` negative, so
nothing is ever idle — a silent permanent stall behind green alerts.

So the service keeps ``time.monotonic()`` beside the wall clock and, on pass entry,
compares the delta since the LAST check. Wall and monotonic advance together in
normal operation (their difference stays ~0); a clock STEP shows up as a large
difference because ``CLOCK_MONOTONIC`` does not jump (on Linux it does not even
advance across suspend, so a resume-from-suspend forward jump is caught). When the
step exceeds ``PASS_INTERVAL_MIN`` the pass is NOT executed, the delta is exported
(``curator_clock_step_seconds``, Фаза 12) and a ``snapshot_request`` goes to every
instance — the snapshot carries ``ageMs``, i.e. client deltas that never saw the
step, rebasing the whole mirror. The guard re-baselines every check, so the NEXT
pass runs normally instead of aborting forever.

**Continuity break.** The first pass after a break in continuity runs only as a
``dry_run`` awaiting a click (the same ``resume_pending`` mechanism a pause expiry
uses). Breaks: DB restored from backup, a fresh DB, a service version rollback, a
lowered ``IDLE_MINUTES``, the clock step above. Detected without new state: a
fingerprint ``{db_uuid, user_version, IDLE_MINUTES, MAIN_INSTANCE_ID}`` of the last
pass is kept in ``settings``; a mismatch on entry arms ``resume_pending``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from src.db.settings_store import get_setting, set_setting

_FINGERPRINT_KEY = "curator_continuity_fingerprint"
_DB_UUID_KEY = "curator_db_uuid"


class ClockGuard:
    """Stateful wall-vs-monotonic comparator. Injectable clocks make it testable.

    ``threshold_s`` is ``PASS_INTERVAL_MIN`` in seconds. :meth:`check` returns the
    skew in seconds since the previous check and re-baselines; the caller aborts the
    pass when ``abs(skew) > threshold_s``.
    """

    def __init__(self, threshold_s: float, *, wall=None, mono=None):
        self._threshold = threshold_s
        self._wall = wall or (lambda: time.time())
        self._mono = mono or (lambda: time.monotonic())
        self._last_wall = self._wall()
        self._last_mono = self._mono()

    def check(self) -> float:
        """Return the wall-vs-monotonic skew (seconds) since the last check, and
        re-baseline so the next pass measures from here."""
        w = self._wall()
        m = self._mono()
        skew = (w - self._last_wall) - (m - self._last_mono)
        self._last_wall = w
        self._last_mono = m
        return skew

    def exceeds(self, skew: float) -> bool:
        return abs(skew) > self._threshold


# --- continuity fingerprint --------------------------------------------------
def current_fingerprint(
    conn: sqlite3.Connection, *, idle_minutes: int, main_instance_id: str
) -> dict:
    """Compute the continuity fingerprint of the CURRENT world (a reader ``fn``).

    ``db_uuid`` is generated once and stored in ``settings`` — an ABSENT one means a
    fresh DB (a break). ``user_version`` catches a service/schema version rollback;
    ``IDLE_MINUTES`` and ``MAIN_INSTANCE_ID`` catch a config change that would make a
    quiet fleet abruptly idle or re-home. NOTE: a DB restored from backup carries the
    backup's own ``db_uuid`` in ``settings``, so ``db_uuid`` alone does not detect a
    backup-restore; ``user_version`` and the config components do when they differ,
    and the clock guard covers the clock-step break. A dedicated external restore
    marker is deferred (see report).

    TODO(Фаза 13): WARNING 2 — restore-from-backup is NOT reliably detected as a
    continuity break, because the backup carries its OWN ``db_uuid`` inside ``settings``
    so a same-version restore looks continuous (live ``relocate`` rows are stale, rules
    rolled back). The fix is an EXTERNAL restore marker (a file/env fingerprint written
    outside the DB by the Фаза 13 deploy/restore procedure) compared here; it cannot be
    a DB-internal value because any DB-internal value travels inside the backup.
    """
    db_uuid = get_setting(conn, _DB_UUID_KEY)
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {
        "db_uuid": db_uuid,  # None on a fresh DB
        "user_version": int(user_version),
        "idle_minutes": int(idle_minutes),
        "main_instance_id": main_instance_id,
    }


def ensure_db_uuid(conn: sqlite3.Connection) -> str:
    """Return the DB's uuid, generating+storing it on first ever call (a writer
    ``fn``). Called once continuity has been established for this pass."""
    existing = get_setting(conn, _DB_UUID_KEY)
    if existing:
        return existing
    new = str(uuid.uuid4())
    set_setting(conn, _DB_UUID_KEY, new)
    return new


def read_stored_fingerprint(conn: sqlite3.Connection) -> dict | None:
    raw = get_setting(conn, _FINGERPRINT_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def store_fingerprint(conn: sqlite3.Connection, fp: dict) -> None:
    """Persist the fingerprint of a pass that ran with established continuity
    (a writer ``fn``). ``db_uuid`` is materialized here so the NEXT pass sees it."""
    fp = dict(fp)
    if not fp.get("db_uuid"):
        fp["db_uuid"] = ensure_db_uuid(conn)
    set_setting(conn, _FINGERPRINT_KEY, json.dumps(fp, sort_keys=True))


def is_continuity_break(stored: dict | None, current: dict) -> bool:
    """A break iff there is a stored fingerprint AND it differs from the current
    one. NO stored fingerprint is NOT itself treated as a break here — a fresh DB's
    very first pass is handled by the runner's own first-run policy, so an empty
    fleet on a new install does not sit forever awaiting a click. (The fresh-DB break
    §7 lists is the case where the DB was cleared but a fingerprint had existed; that
    is a genuine mismatch and IS caught.)"""
    if stored is None:
        return False
    # Compare only the components both carry; a stored db_uuid that differs from the
    # live one (restore over a populated settings table) is a break.
    for key in ("user_version", "idle_minutes", "main_instance_id"):
        if stored.get(key) != current.get(key):
            return True
    live_uuid = current.get("db_uuid")
    if live_uuid is not None and stored.get("db_uuid") != live_uuid:
        return True
    return False
