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
fingerprint ``{db_uuid, user_version, IDLE_MINUTES, MAIN_INSTANCE_ID, restore_marker}``
of the last pass is kept in ``settings``; a mismatch on entry arms ``resume_pending``.
A MISSING fingerprint is the "fresh DB" break — see :func:`is_continuity_break` for
where the first-run boundary runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from src.db.settings_store import get_setting, set_setting

_FINGERPRINT_KEY = "curator_continuity_fingerprint"
_DB_UUID_KEY = "curator_db_uuid"

# The components compared for a break. ``db_uuid`` is compared separately (a live
# ``None`` means "not materialized yet", which is not by itself a mismatch).
_COMPARED_KEYS = ("user_version", "idle_minutes", "main_instance_id", "restore_marker")

# Sentinel for "RESTORE_MARKER_PATH is configured, the file is simply NOT THERE".
# A stable, meaningful state (distinct from ``None`` = not configured), so a marker that
# DISAPPEARS breaks exactly once and a persistently absent one does not re-break.
# In the container this state became hard to reach — ``entrypoint.sh`` creates the marker
# on every start when it is absent — but it is NOT dead code: the file can be deleted (or
# its directory emptied) while the service runs, and a pass in that window must record a
# state rather than crash. Keep it.
MARKER_MISSING = "missing"

# Sentinel for "the read FAILED" (mount wedged, EACCES, EIO). NOT a state: it is the
# absence of knowledge, so it never participates in a comparison and never overwrites a
# known digest — see :func:`is_continuity_break` and :func:`store_fingerprint`. A
# transient fault must not manufacture a break, because the resulting ``resume_pending``
# latch can only be cleared by a real pass, which the latch itself blocks: the service
# would need a human click to leave a state a flaky mount put it in.
MARKER_UNREADABLE = "unreadable"

# The marker is a short token (a uuid); read at most this much of it. A typo in
# RESTORE_MARKER_PATH pointing at a backup or a log must not pull an arbitrarily large
# file into memory on every pass.
_MARKER_READ_LIMIT = 4096

# The marker read gets its OWN single-thread executor, never the loop's default one.
# ``Database.read`` runs on the default executor (``asyncio.to_thread``), and that pool
# is min(32, cpu+4) threads — six on a two-core container. The marker sits on a volume
# (the same one as the DB, or a mount of its own), and a wedged mount makes ``open()``
# an uninterruptible syscall: on the shared pool, one stuck read per pass trigger (the
# driver, POST /api/run_pass, the pass
# DELETE /api/pause runs, MCP) would exhaust the pool within minutes and every
# ``Database.read`` — the whole HTTP API, /metrics, /api/state, MCP — would block behind
# it. Isolated here, the damage is capped at this one thread; later reads queue behind it
# and simply time out into MARKER_UNREADABLE, and the rest of the service is untouched.
# (A wedged worker is not killable from Python, so it can also delay interpreter exit;
# that is strictly better than wedging the live service, and the container's stop grace
# period covers it.)
_MARKER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restore-marker")

# Reading a local token file is sub-millisecond; anything near this is a sick mount, and
# waiting longer only delays the pass. Not an ENV knob: it is a safety bound on a syscall,
# not something an operator tunes.
_MARKER_READ_TIMEOUT_S = 2.0


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


# --- the EXTERNAL restore marker (WARNING 2) ---------------------------------
def read_restore_marker(path: str | None) -> str | None:
    """Digest the external restore marker, or ``None`` when it is not configured.

    **Why it cannot live in the DB.** A DB restored from backup carries the backup's
    OWN ``db_uuid`` inside ``settings``, so no DB-internal value can ever detect a
    restore: a same-version restore looks perfectly continuous while its live
    ``relocate`` rows are stale and its rules are yesterday's. The marker therefore
    has to sit OUTSIDE the file that gets restored.

    **Operator contract.** ``RESTORE_MARKER_PATH`` names a small file that is NOT the DB
    and NOT part of a DB backup copy (in the shipped deployment it sits beside them on
    the same volume, ``/app/data/restore/continuity-marker`` — the separation that
    matters is at file level, since the documented procedure restores the DB *file*).
    Its content is a fresh uuid written on EVERY restore, e.g.::

        uuidgen > "$RESTORE_MARKER_PATH"      # after each restore

    The container creates the file itself on first start (``entrypoint.sh``), with a
    fresh uuid and never overwriting an existing one — so at install there is nothing to
    do. Rewriting it on a restore stays an OPERATOR action and cannot be automated: the
    service has no way to know it was rolled back, which is the whole reason the marker
    is external.

    Only the CHANGE matters, so any stable-then-rewritten token works; a uuid is just
    the cheapest collision-free one. (The restore procedure itself is documented in
    ``deploy/DEPLOY.md``.) A restored DB then meets a marker whose digest differs from
    the one its stored fingerprint recorded => continuity break => the first pass is a
    ``dry_run`` awaiting a click.

    **Blocking I/O — call it OUTSIDE any ``Database`` transaction.** A wedged mount turns
    ``open()`` into an uninterruptible syscall; run inside a writer ``fn(conn)`` that
    would hold the single writer thread and the global write lock for the duration. The
    runner reads it ONCE per pass, in a worker thread, and passes the VALUE down (which
    also guarantees the pass compares and stores the same digest — reading twice could
    swallow a break landing between).

    Return values — each is a recorded STATE, and a break is a CHANGE between two of
    them (never "the value is bad"):

    * empty / unset path -> ``None``, i.e. "no marker configured". NOTE this is a value,
      not an absent component: :func:`current_fingerprint` always writes the key. An
      installation that stays unconfigured therefore never breaks (``None`` == ``None``),
      but TURNING THE DETECTOR ON OR OFF is a change like any other fingerprint input and
      costs one dry_run + click — deliberately, see :func:`is_continuity_break`.
    * configured and readable -> a sha256 of the file's first :data:`_MARKER_READ_LIMIT`
      bytes (the file may hold anything; hashing keeps the ``settings`` row small).
    * configured, file NOT THERE -> :data:`MARKER_MISSING` — a real observation, compared
      like any other. A marker that DISAPPEARS breaks once; a marker that stays absent
      does not break again; a file APPEARING over a recorded "missing" breaks once too.
    * configured, read FAILED -> :data:`MARKER_UNREADABLE` — not a state but a gap in
      knowledge; comparison and storage both ignore it (a flaky mount must not arm a
      latch only a human can clear).
    """
    if not path or not str(path).strip():
        return None
    try:
        with open(str(path).strip(), "rb") as fh:
            return hashlib.sha256(fh.read(_MARKER_READ_LIMIT)).hexdigest()
    except FileNotFoundError:
        # A stable, meaningful observation: the marker is genuinely not there.
        logger.warning("curator: restore marker {} is missing", path)
        return MARKER_MISSING
    except OSError as exc:
        # Never let a marker read fault crash a pass — and never let it invent a break.
        logger.warning("curator: restore marker {} unreadable: {}", path, exc)
        return MARKER_UNREADABLE


async def read_restore_marker_async(
    path: str | None, *, timeout_s: float = _MARKER_READ_TIMEOUT_S
) -> str | None:
    """:func:`read_restore_marker` off the event loop, on its own thread, time-bounded.

    This is the ONLY entry point a pass should use. Two guarantees the bare sync version
    cannot give: the blocking ``open()`` never runs on the event loop or on the shared
    executor ``Database.read`` needs (see :data:`_MARKER_EXECUTOR`), and it never waits
    forever — a read that outlives ``timeout_s`` degrades to :data:`MARKER_UNREADABLE`,
    which by construction is the "no information" value that changes no verdict.

    The wedged worker thread cannot be killed, so it stays parked; subsequent passes
    queue behind it and time out in turn. That is the intended failure shape: the restore
    detector goes blind (and says so — the runner publishes it for /metrics) while every
    other part of the service keeps running.
    """
    if not path or not str(path).strip():
        return None
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_MARKER_EXECUTOR, read_restore_marker, path)
    try:
        return await asyncio.wait_for(fut, timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "curator: restore marker {} read timed out after {}s — treating as unreadable",
            path, timeout_s,
        )
        return MARKER_UNREADABLE


# --- continuity fingerprint --------------------------------------------------
def current_fingerprint(
    conn: sqlite3.Connection,
    *,
    idle_minutes: int,
    main_instance_id: str,
    restore_marker: str | None = None,
) -> dict:
    """Compute the continuity fingerprint of the CURRENT world (a reader ``fn``).

    ``db_uuid`` is generated once and stored in ``settings`` — an ABSENT one means a
    fresh DB. ``user_version`` catches a service/schema version rollback;
    ``IDLE_MINUTES`` and ``MAIN_INSTANCE_ID`` catch a config change that would make a
    quiet fleet abruptly idle or re-home; ``restore_marker`` catches a
    restore-from-backup (it is the only component that does NOT travel inside the
    backup). The clock guard covers the clock-step break separately.

    ``restore_marker`` is the already-read VALUE, not a path: this function runs inside
    a DB transaction and must not touch the filesystem (see :func:`read_restore_marker`).
    """
    db_uuid = get_setting(conn, _DB_UUID_KEY)
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {
        "db_uuid": db_uuid,  # None on a fresh DB
        "user_version": int(user_version),
        "idle_minutes": int(idle_minutes),
        "main_instance_id": main_instance_id,
        "restore_marker": restore_marker,
    }


def fleet_populated(conn: sqlite3.Connection) -> bool:
    """Does this DB already describe a fleet (>= 1 instance OR >= 1 tab)? (a reader ``fn``)

    This is the first-run boundary of :func:`is_continuity_break`; see there.
    """
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM instances) OR EXISTS(SELECT 1 FROM tabs)"
    ).fetchone()
    return bool(row[0])


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
    if fp.get("restore_marker") == MARKER_UNREADABLE:
        # The read failed; that is not an observation about the marker. Carry the last
        # KNOWN digest forward so a genuine change is still detected once the mount is
        # back — storing the sentinel would erase the only value we can compare against.
        previous = read_stored_fingerprint(conn) or {}
        if "restore_marker" in previous:
            fp["restore_marker"] = previous["restore_marker"]
        else:
            fp.pop("restore_marker")
    set_setting(conn, _FINGERPRINT_KEY, json.dumps(fp, sort_keys=True))


def _marker_comparable(stored: dict, current: dict) -> bool:
    """May the ``restore_marker`` component take part in the break comparison?

    Two cases where it may not, and both would otherwise produce a break nobody can
    clear without a click (the ``resume_pending`` latch blocks the very pass that would
    lift it):

    * **The stored fingerprint predates the component.** An installation upgraded into a
      release that sets ``RESTORE_MARKER_PATH`` has a fingerprint with no marker key at
      all; comparing a digest against "absent" would flag every existing install as
      restored on its first pass. The first pass stores the digest, and from the next one
      the component is compared normally. The cost is a one-pass blind spot: a restore
      performed in exactly that window is not caught BY THE MARKER (the other components
      still apply).
    * **The current read FAILED** (:data:`MARKER_UNREADABLE`) — a wedged or not-yet-
      mounted volume is a gap in knowledge, not evidence of a restore.

    :data:`MARKER_MISSING` is NOT in this list: "the file is not there" is a genuine
    observation and stays comparable.

    Neither is ``None`` ("no marker configured"). Turning ``RESTORE_MARKER_PATH`` on or
    off is therefore a break, exactly like lowering ``IDLE_MINUTES`` or changing
    ``MAIN_INSTANCE_ID`` — the other config inputs of this fingerprint, which §7 already
    lists as breaks. That is the point: a silent DISABLE would drop restore detection
    with no signal at all, and the two directions are not worth separating when the price
    of both being loud is one dry_run the operator asked for by editing the config.
    """
    if "restore_marker" not in stored:
        return False
    return current.get("restore_marker") != MARKER_UNREADABLE


def is_continuity_break(
    stored: dict | None, current: dict, *, fleet_populated: bool = False
) -> bool:
    """Is this pass the first one after a break in continuity? (§7)

    With a stored fingerprint: a break iff it differs from the current one.

    **With NO stored fingerprint the ``fleet_populated`` flag draws the boundary** —
    this is the "чистая БД" break §7 lists, and it is real: on a clean DB every tab
    arrives with ``age_unknown=1`` and ``last_active_at = now``, so one hour later the
    WHOLE fleet becomes eligible simultaneously and a single unconfirmed pass would
    drain it. So:

    * fingerprint absent AND the DB already describes a fleet (>= 1 instance or tab,
      i.e. a browser has connected) => BREAK: arm ``resume_pending``, show the plan,
      wait for one click.
    * fingerprint absent AND the DB is completely empty (a brand-new install whose
      browser has never connected) => NOT a break: there is nothing to drain and
      nothing to look at, so making a new owner hunt for a button would be noise.

    The runner keeps that boundary honest from the other side: it stores a fingerprint
    only for a pass that actually saw a fleet, so an empty-DB pass cannot silently
    consume the first-run latch before the browser ever connects.
    """
    if stored is None:
        return bool(fleet_populated)
    # Compare only the components both carry; a stored db_uuid that differs from the
    # live one (restore over a populated settings table) is a break.
    for key in _COMPARED_KEYS:
        if key == "restore_marker" and not _marker_comparable(stored, current):
            continue
        if stored.get(key) != current.get(key):
            return True
    live_uuid = current.get("db_uuid")
    if live_uuid is not None and stored.get("db_uuid") != live_uuid:
        return True
    return False
