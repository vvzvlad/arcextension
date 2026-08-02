"""``GET /metrics`` — Prometheus exposition for the curator (§12).

Design decisions taken straight from §12 "Наблюдаемость":

* A **separate** ``METRICS_TOKEN`` Bearer guards this endpoint (never ``EXT_TOKEN``):
  the scrape credential lives in git plaintext, so it must never be able to touch
  ``/ext`` / ``/api/*`` / ``/mcp``.
* **Served in degraded mode.** The handler never trusts the schema: a failed DB read
  degrades to defaults and still emits every metric (with ``curator_migration_failed=1``).
* **Everything about a pass is computed at scrape time from the ``passes`` table** —
  never from process memory and never from ``max(actions.ts)``. A gauge registered at
  ``0`` on start would, in a crash-loop, read "healthy" on every restart and the
  "no pass happened" rule would never fire in exactly the outage it exists for.
* **Every §12 metric is emitted on every scrape** (its HELP/TYPE lines are always
  present, scalars always carry a value). Per-label families (``{id}`` / ``{kind}`` /
  ``{to_instance}``) legitimately have zero series when nothing exists — that absence
  is handled by ``noDataState: OK`` on the per-metric alert rules.
* **Pause suppression lives in the GAUGE, not the alert expression** (§12): while
  paused, ``curator_pass_overdue_seconds`` and ``curator_instance_snapshot_age_seconds``
  read 0, so the routine hour-long pause cannot light "half-open socket" / "overdue".

The Prometheus text format is hand-rolled (a tiny :class:`MetricsRegistry`) rather
than pulling in ``prometheus_client``: the pass/instance gauges are computed from the
DB at scrape time, so there is nothing to hold in a client registry — a custom
renderer is the clean fit and stays unit-testable.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from starlette.requests import Request
from starlette.responses import Response

from src.api.auth_metrics import auth_rejections
from src.api.guards import require_metrics_token
from src.curator.enroll import ENROLL_WINDOW_UNTIL_KEY

# Prometheus text exposition content type (§12).
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Backup filenames written by src.db.backup: finished copies are ``curator-*.db``;
# in-flight copies are ``curator-*.db.tmp`` and are deliberately NOT matched here
# (§12: read the newest NON-.tmp copy).
_BACKUP_GLOB = "curator-*.db"

# The runtime settings keys read at scrape time.
_PAUSE_UNTIL_KEY = "pause_until"
_RESUME_PENDING_KEY = "resume_pending"
# Label value for a deferred row whose ``decision`` is NULL/blank — rows written before
# the cause split landed. They are real deferrals and must keep their count, but an empty
# label value is not a legal answer (it reads as "no cause" rather than "not recorded").
_DEFERRED_CAUSE_UNKNOWN = "unknown"
# Persisted by the runner when the server-clock guard trips (see src.curator.runner):
# the last observed wall-vs-monotonic step in seconds. 0/absent means "never stepped".
CLOCK_STEP_KEY = "curator_clock_step_seconds"
# Persisted by ``runner._marker()`` (best-effort, same shape as CLOCK_STEP_KEY): "1" when
# the CONFIGURED restore marker could not be read on the last pass that needed it, "0"
# when it was read or none is configured. An ABSENT row means no pass has looked yet and
# reads as 0 — never as a failure.
RESTORE_MARKER_UNREADABLE_KEY = "curator_restore_marker_unreadable"


# --- the hand-rolled Prometheus text renderer -------------------------------
def _escape_label_value(value: str) -> str:
    # Prometheus label-value escaping: backslash, double-quote, newline.
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float | int) -> str:
    # Bools first (a bool is an int subclass); integers stay integer-formatted;
    # floats render without scientific notation for the ranges we produce.
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    fvalue = float(value)
    # Prometheus exposition spells non-finite values +Inf/-Inf/NaN (not Python's
    # inf/nan). Our only float source is clock_step_seconds read from settings, so a
    # foreign write of a non-finite string must not emit an invalid sample.
    if not math.isfinite(fvalue):
        if fvalue != fvalue:  # NaN
            return "NaN"
        return "+Inf" if fvalue > 0 else "-Inf"
    return repr(fvalue)


class MetricsRegistry:
    """Accumulates ``# HELP`` / ``# TYPE`` / sample lines for one scrape.

    Each metric family is emitted with its HELP and TYPE lines UNCONDITIONALLY, so
    the metric name is present on every scrape even when it has zero samples (an
    empty ``{id}`` family). Scalars pass a single ``({}, value)`` sample.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def metric(
        self,
        name: str,
        help_text: str,
        mtype: str,
        samples: list[tuple[dict[str, str], float | int]],
    ) -> None:
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            if labels:
                rendered = ",".join(
                    f'{k}="{_escape_label_value(str(v))}"' for k, v in labels.items()
                )
                self._lines.append(f"{name}{{{rendered}}} {_format_value(value)}")
            else:
                self._lines.append(f"{name} {_format_value(value)}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


# --- the scrape-time DB snapshot --------------------------------------------
@dataclass
class InstanceRow:
    id: str
    connected: int
    last_seen_at: int | None
    snapshot_at: int | None


@dataclass
class Snapshot:
    """Everything read from the DB in one reader connection at scrape time."""

    # Newest pass (any) — for last_pass_ts and the actions-of-last-pass grouping.
    newest_pass_id: str | None = None
    newest_started_at: int | None = None
    newest_finished_at: int | None = None
    # Newest FINISHED pass — for last_pass_ok / overdue reference.
    finished_at: int | None = None
    finished_ok: int | None = None
    finished_instances_ready: int | None = None
    # Oldest started_at across all passes — the overdue anchor when nothing finished.
    oldest_started_at: int | None = None
    actions_by_kind: dict[str, int] = field(default_factory=dict)
    instances: list[InstanceRow] = field(default_factory=list)
    rules_total: int = 0
    rules_invalid: int = 0
    quarantined_total: int = 0
    relocations_incomplete: int = 0
    # {(instance_to, cause): count} — the cause is ``actions.decision`` (§7's
    # ``target_not_ready`` vs the runner's ``dup_same_pass``); see ``_collect``.
    deferred: dict[tuple[str, str], int] = field(default_factory=dict)
    pause_until: int | None = None
    resume_pending: bool = False
    # Absolute deadline (epoch ms) of an armed enrollment window, or None when no window
    # is armed (§8). Read straight from `settings` at scrape time, exactly like
    # ``pause_until`` — the signed remaining seconds are computed against ``now`` in
    # ``_enroll_window_seconds_remaining`` so a restart is a no-op.
    enroll_window_until: int | None = None
    clock_step_seconds: float = 0.0
    # Default False, and that default is the answer for a brand-new install: "no pass has
    # looked at the marker yet" is not "the marker is broken" (§12).
    restore_marker_unreadable: bool = False
    main_never_seen: bool = True


def _collect(conn: sqlite3.Connection, main_instance_id: str) -> Snapshot:
    """Read the whole scrape snapshot in ONE reader connection (a ``Database.read``
    ``fn(conn)``): no ``await`` inside, parameterized SQL, static SELECTs."""
    snap = Snapshot()

    newest = conn.execute(
        "SELECT pass_id, started_at, finished_at FROM passes "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if newest is not None:
        snap.newest_pass_id = newest[0]
        snap.newest_started_at = newest[1]
        snap.newest_finished_at = newest[2]

    finished = conn.execute(
        "SELECT finished_at, ok, instances_ready FROM passes "
        "WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if finished is not None:
        snap.finished_at = finished[0]
        snap.finished_ok = finished[1]
        snap.finished_instances_ready = finished[2]

    row = conn.execute("SELECT MIN(started_at) FROM passes").fetchone()
    snap.oldest_started_at = row[0] if row is not None else None

    # Per-kind action counts of the LAST pass. Scoping to the newest pass_id makes
    # the "all labels reset to 0 at the start of each pass" behaviour free (§12): a
    # new pass has a new pass_id, so the previous pass's kinds simply do not appear.
    if snap.newest_pass_id is not None:
        for kind, count in conn.execute(
            "SELECT kind, COUNT(*) FROM actions WHERE pass_id = ? GROUP BY kind",
            (snap.newest_pass_id,),
        ).fetchall():
            snap.actions_by_kind[kind] = int(count)

        # Per-target, per-CAUSE deferred backlog of the last pass. One aggregated row
        # per (pass_id, instance_to, decision) carries the count in ``reason``
        # (src.curator.runner._write_deferred).
        #
        # The cause has to ride along as its own label, because the two the runner
        # distinguishes are not the same kind of event: ``target_not_ready`` means the
        # home is unreachable (§7 connectivity — somebody has to fix something), while
        # ``dup_same_pass`` is the curator serialising itself so it does not open a
        # duplicate within one pass (routine, healthy, self-resolving next pass). Summed
        # together they cannot be alerted on or even read: a rising number says nothing
        # about whether anything is wrong.
        # Grouped in PYTHON, not with a SQL ``GROUP BY``: ``reason`` is a TEXT column, so
        # ``SUM(CAST(reason AS INTEGER))`` would silently turn a malformed value into 0
        # and lose the series, whereas the guarded ``int()`` below keeps the (target,
        # cause) pair visible at 0. It also stays correct if a pass ever writes more than
        # one row for the same pair.
        for instance_to, decision, reason in conn.execute(
            "SELECT instance_to, decision, reason FROM actions "
            "WHERE pass_id = ? AND kind = 'relocate' AND status = 'deferred'",
            (snap.newest_pass_id,),
        ).fetchall():
            if instance_to is None:
                continue
            # Rows written before the cause split (and any future writer that forgets)
            # carry NULL/blank: they are real deferrals and must still be counted, but
            # never as an empty or garbage label value.
            cause = decision if decision else _DEFERRED_CAUSE_UNKNOWN
            key = (instance_to, cause)
            try:
                snap.deferred[key] = snap.deferred.get(key, 0) + int(reason)
            except (TypeError, ValueError):
                snap.deferred[key] = snap.deferred.get(key, 0)

    for iid, connected, last_seen_at, snapshot_at in conn.execute(
        "SELECT id, connected, last_seen_at, snapshot_at FROM instances ORDER BY id"
    ).fetchall():
        snap.instances.append(
            InstanceRow(
                id=iid,
                connected=int(connected),
                last_seen_at=last_seen_at,
                snapshot_at=snapshot_at,
            )
        )

    rc = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN invalid = 1 THEN 1 ELSE 0 END), 0) "
        "FROM rules"
    ).fetchone()
    snap.rules_total = int(rc[0])
    snap.rules_invalid = int(rc[1])

    # Active quarantine rows only (§12: "active quarantine rows").
    now_ms = int(time.time() * 1000)
    snap.quarantined_total = int(
        conn.execute(
            "SELECT COUNT(*) FROM quarantine WHERE until > ?", (now_ms,)
        ).fetchone()[0]
    )

    # Live relocations without a done phase-B close — the §7/§8 "live relocation"
    # shape from src.curator.mirror.load_mirror, reduced to a count.
    snap.relocations_incomplete = int(
        conn.execute(
            "SELECT COUNT(*) FROM actions a "
            "WHERE a.kind = 'relocate' AND a.status = 'done' AND a.restored_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM actions rc "
            "WHERE rc.kind = 'relocate_close' AND rc.status = 'done' "
            "AND rc.origin_action_id = a.id)"
        ).fetchone()[0]
    )

    pause_row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (_PAUSE_UNTIL_KEY,)
    ).fetchone()
    if pause_row is not None and pause_row[0] not in (None, ""):
        try:
            snap.pause_until = int(pause_row[0])
        except (TypeError, ValueError):
            snap.pause_until = None

    resume_row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (_RESUME_PENDING_KEY,)
    ).fetchone()
    snap.resume_pending = bool(resume_row is not None and resume_row[0])

    # Enrollment-window deadline (§8). Absent/blank/garbage reads as "no window armed"
    # (None) — never a spurious 0 deadline, mirroring how ``pause_until`` is decoded.
    enroll_row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (ENROLL_WINDOW_UNTIL_KEY,)
    ).fetchone()
    if enroll_row is not None and enroll_row[0] not in (None, ""):
        try:
            snap.enroll_window_until = int(enroll_row[0])
        except (TypeError, ValueError):
            snap.enroll_window_until = None

    step_row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (CLOCK_STEP_KEY,)
    ).fetchone()
    if step_row is not None and step_row[0] not in (None, ""):
        try:
            snap.clock_step_seconds = float(step_row[0])
        except (TypeError, ValueError):
            snap.clock_step_seconds = 0.0

    # Restore-marker readability. Only the exact "1" is a failure: an absent row (no pass
    # has needed the marker yet), a blank, or anything unparseable reads as 0, because
    # the ONE thing this gauge must never do is invent an outage out of missing evidence.
    marker_row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (RESTORE_MARKER_UNREADABLE_KEY,)
    ).fetchone()
    snap.restore_marker_unreadable = bool(
        marker_row is not None and str(marker_row[0]).strip() == "1"
    )

    # main_instance_never_seen: no row for MAIN_INSTANCE_ID, or last_seen_at IS NULL
    # (§12: the sticky, sleep-independent "stock branch is silently off" fact).
    main_row = conn.execute(
        "SELECT last_seen_at FROM instances WHERE id = ?", (main_instance_id,)
    ).fetchone()
    snap.main_never_seen = main_row is None or main_row[0] is None

    return snap


# --- backup dir reading (filesystem, not the DB) ----------------------------
def _newest_backup(backup_dir: str) -> Path | None:
    """The newest finished (non-``.tmp``) backup copy, or None. Timestamped names
    sort chronologically, so ``max`` over the glob is the newest."""
    try:
        copies = sorted(Path(backup_dir).glob(_BACKUP_GLOB))
    except OSError:
        return None
    return copies[-1] if copies else None


# --- overdue / age computations (pause suppression lives here) --------------
def _pass_overdue_seconds(snap: Snapshot, now_ms: int, interval_s: int, paused: bool) -> int:
    """Seconds the next pass is overdue (§12). 0 while paused. The reference is the
    last FINISHED pass; if none ever finished, the OLDEST started_at is the anchor
    so a crash-loop (many started, none finished) trips and — being read from
    ``passes`` — is NOT reset by a fresh process. Zero pass rows at all => 0 (a truly
    fresh install has no evidence a pass was ever due).

    **After a pause EXPIRES the clock restarts from the expiry moment**, not from
    ``last_pass_ts`` (§7: «`curator_pass_overdue_seconds` после истечения паузы
    считается от момента истечения, а не от `last_pass_ts`»). Otherwise the hour-long
    pause the gauge suppressed reappears as overdue the very second it lapses and the
    alert fires immediately — while §7 promises a full 3×``PASS_INTERVAL`` for the human
    to see the pending plan and click. ``pause_until`` survives a timeout expiry
    (only a manual resume / confirm clears it), so it is exactly the "when did the stop
    lapse" anchor; a resumed pause has no row and changes nothing here.
    """
    if paused:
        return 0
    if snap.finished_at is not None:
        reference_ms = snap.finished_at
    elif snap.oldest_started_at is not None:
        reference_ms = snap.oldest_started_at
    else:
        return 0
    # `paused` is False here, so a present pause_until is necessarily in the past.
    if snap.pause_until is not None and snap.pause_until > reference_ms:
        reference_ms = snap.pause_until
    overdue = (now_ms - reference_ms) // 1000 - interval_s
    return overdue if overdue > 0 else 0


def _enroll_window_seconds_remaining(snap: Snapshot, now_ms: int) -> int:
    """SIGNED whole seconds until the enrollment window's deadline (§8).

    * No window armed (absent/blank/garbage deadline) -> EXACTLY 0.
    * Armed and still open (deadline in the future) -> POSITIVE seconds remaining,
      rounded UP so a sub-second-but-open window still reads >= 1 and never collides
      with the "no window" 0 (matches :func:`src.curator.enroll._ceil_seconds`).
    * Armed but PAST its deadline and not yet closed -> NEGATIVE seconds overdue
      (symmetric ceil of the magnitude, so always <= -1) — the §37 alert keys on ``< 0``.
    * Exactly AT the deadline (remaining == 0) -> 0, deliberately indistinguishable from
      "no window"; both are benign and neither is the armed-overdue state the alert wants.

    Read from the DB at scrape time, so a restart re-reads the SAME stored deadline and
    changes nothing; a degraded scrape (no snapshot) leaves ``enroll_window_until`` None
    and reports 0 (no window), never a 500.
    """
    until = snap.enroll_window_until
    if until is None:
        return 0
    remaining_ms = until - now_ms
    if remaining_ms == 0:
        return 0
    if remaining_ms > 0:
        return (remaining_ms + 999) // 1000
    return -((-remaining_ms + 999) // 1000)


def _render(snap: Snapshot, settings, now_ms: int, degraded: bool) -> str:
    reg = MetricsRegistry()
    interval_s = int(settings.pass_interval_min) * 60
    paused = snap.pause_until is not None and snap.pause_until > now_ms

    # --- pass facts (from `passes`) -----------------------------------------
    last_pass_ts = (
        snap.newest_finished_at
        if snap.newest_finished_at is not None
        else (snap.newest_started_at if snap.newest_started_at is not None else 0)
    )
    reg.metric(
        "curator_last_pass_ts",
        "Server-clock ms of the newest pass (finished_at, else started_at); 0 if none.",
        "gauge",
        [({}, int(last_pass_ts))],
    )

    # last_pass_ok: ok of the newest FINISHED pass, forced to 0 when that pass found
    # zero ready instances (§12 "зелено и мертво"); 0 if no finished pass yet.
    if snap.finished_at is None:
        last_pass_ok = 0
    elif snap.finished_ok and (snap.finished_instances_ready or 0) > 0:
        last_pass_ok = 1
    else:
        last_pass_ok = 0
    reg.metric(
        "curator_last_pass_ok",
        "1 iff the newest finished pass succeeded with >=1 ready instance; else 0.",
        "gauge",
        [({}, last_pass_ok)],
    )

    reg.metric(
        "curator_actions_last_pass",
        "Per-kind action-row count of the last pass (reset to 0 for absent kinds).",
        "gauge",
        [({"kind": kind}, count) for kind, count in sorted(snap.actions_by_kind.items())],
    )

    reg.metric(
        "curator_pass_overdue_seconds",
        "Seconds the next pass is overdue (0 while paused; from `passes`, not memory).",
        "gauge",
        [({}, _pass_overdue_seconds(snap, now_ms, interval_s, paused))],
    )

    # --- instances ----------------------------------------------------------
    connected_samples: list[tuple[dict[str, str], int]] = []
    last_seen_samples: list[tuple[dict[str, str], int]] = []
    absent_samples: list[tuple[dict[str, str], int]] = []
    snapshot_age_samples: list[tuple[dict[str, str], int]] = []
    for inst in snap.instances:
        labels = {"id": inst.id}
        connected_samples.append((labels, 1 if inst.connected else 0))
        last_seen_samples.append((labels, int(inst.last_seen_at or 0)))
        # absent_seconds: 0 while connected; else seconds since last hello (huge when
        # never seen — that instance is covered separately by main_instance_never_seen).
        if inst.connected:
            absent = 0
        else:
            absent = max(0, (now_ms - int(inst.last_seen_at or 0)) // 1000)
        absent_samples.append((labels, absent))
        # snapshot_age_seconds: 0 while paused (suppression in the gauge, §12); else
        # seconds since the instance's mirror snapshot (huge when never snapshotted).
        if paused:
            age = 0
        else:
            age = max(0, (now_ms - int(inst.snapshot_at or 0)) // 1000)
        snapshot_age_samples.append((labels, age))

    reg.metric(
        "curator_instance_connected",
        "1/0 live-connection flag per instance (NOT alertable: laptops sleep nightly).",
        "gauge",
        connected_samples,
    )
    reg.metric(
        "curator_instance_last_seen_ts",
        "Server-clock ms of the instance's last successful hello; 0 if never.",
        "gauge",
        last_seen_samples,
    )
    reg.metric(
        "curator_instance_absent_seconds",
        "Seconds since last successful hello per instance; 0 while connected.",
        "gauge",
        absent_samples,
    )
    reg.metric(
        "curator_instance_snapshot_age_seconds",
        "Age (s) of the instance's mirror snapshot; 0 while paused.",
        "gauge",
        snapshot_age_samples,
    )
    reg.metric(
        "curator_main_instance_never_seen",
        "1 iff MAIN_INSTANCE_ID has no instances row or last_seen_at IS NULL.",
        "gauge",
        [({}, 1 if snap.main_never_seen else 0)],
    )

    # --- rules / curation state ---------------------------------------------
    reg.metric(
        "curator_rules_total",
        "Number of curation rules (== 0 disables the whole stock branch).",
        "gauge",
        [({}, snap.rules_total)],
    )
    reg.metric(
        "curator_rules_invalid",
        "Number of rules flagged invalid=1 (orphaned instance_id).",
        "gauge",
        [({}, snap.rules_invalid)],
    )
    # NAME UNCHANGED on purpose: `deploy/alerts.yml` and the series-name guard in
    # `tests/test_deploy_alerts.py` key on it. Only a second LABEL is added, which no
    # existing rule can be broken by — no rule references this metric today, and any
    # future one must aggregate (`sum by (to_instance)`) or select a cause explicitly.
    reg.metric(
        "curator_deferred_total",
        "Deferred tab count from the last pass, by target instance and CAUSE "
        "(target_not_ready = home unreachable, needs attention; dup_same_pass = the "
        "curator serialising itself, routine).",
        "gauge",
        [
            ({"to_instance": to, "cause": cause}, n)
            for (to, cause), n in sorted(snap.deferred.items())
        ],
    )
    reg.metric(
        "curator_quarantined_total",
        "Active (until > now) quarantine rows.",
        "gauge",
        [({}, snap.quarantined_total)],
    )
    reg.metric(
        "curator_relocations_incomplete",
        # DB-level count (relocate/done/not-restored with no done phase-B close). This
        # is deliberately wider than mirror.live_relocations, which also drops rows for
        # a departed instance or a rotated session: a stuck relocation is still
        # incomplete for observability even when the pass no longer acts on it.
        "Relocations without a done phase-B close (DB-level; may include stuck rows).",
        "gauge",
        [({}, snap.relocations_incomplete)],
    )

    # --- pause --------------------------------------------------------------
    reg.metric(
        "curator_paused_until",
        "pause_until (server-clock ms) while paused; 0 if not paused.",
        "gauge",
        [({}, snap.pause_until if paused else 0)],
    )
    reg.metric(
        "curator_resume_pending",
        "1 iff a resume-pending flag is set in settings; else 0.",
        "gauge",
        [({}, 1 if snap.resume_pending else 0)],
    )

    # --- enrollment window (§8) ---------------------------------------------
    # Registered on EVERY scrape (like every other gauge): the sample is always emitted,
    # computed from the stored deadline in `settings`, not process memory — so a restart
    # re-reads the same window and a degraded scrape reports 0 (no window), never a 500.
    reg.metric(
        "curator_enroll_window_seconds_remaining",
        "Signed whole seconds until the enrollment window deadline: >0 while open, <0 "
        "while armed-but-past-its-deadline-and-not-yet-closed. 0 covers BOTH 'no window "
        "armed' AND 'exactly at the deadline' — so an alert distinguishes the "
        "armed-overdue (<0) case from a benign 0.",
        "gauge",
        [({}, _enroll_window_seconds_remaining(snap, now_ms))],
    )

    # --- backup (filesystem) ------------------------------------------------
    newest = _newest_backup(settings.backup_dir)
    if newest is not None:
        try:
            stat = newest.stat()
            mtime_s = int(stat.st_mtime)
            backup_age = max(0, now_ms // 1000 - mtime_s)
            backup_last_ok = mtime_s
            backup_bytes = int(stat.st_size)
        except OSError:
            newest = None
    if newest is None:
        # No backup: age must read LARGE so the "backup too old" rule fires — a 0 age
        # would be the exact "green and dead" trap. last_ok_ts / bytes read 0.
        backup_age = now_ms // 1000
        backup_last_ok = 0
        backup_bytes = 0
    reg.metric(
        "curator_backup_age_seconds",
        "Seconds since the newest backup's mtime; ~now (large) if none.",
        "gauge",
        [({}, backup_age)],
    )
    reg.metric(
        "curator_backup_last_ok_ts",
        "Unix seconds mtime of the newest backup; 0 if none.",
        "gauge",
        [({}, backup_last_ok)],
    )
    reg.metric(
        "curator_backup_bytes",
        "Size in bytes of the newest backup; 0 if none.",
        "gauge",
        [({}, backup_bytes)],
    )

    # --- process / auth -----------------------------------------------------
    reg.metric(
        "curator_migration_failed",
        "1 iff the service is in degraded mode (a migration failed); else 0.",
        "gauge",
        [({}, 1 if degraded else 0)],
    )
    reg.metric(
        "curator_clock_step_seconds",
        "Last observed server-clock step (s) that aborted a pass; 0 if none.",
        "gauge",
        [({}, snap.clock_step_seconds)],
    )
    # Emitted on EVERY scrape, including the very first one on a fresh install (§12): a
    # gauge that only appears once the fault occurs gives NoData for "it never ran", the
    # exact silence this metric exists to break. "Marker configured but unreadable" is
    # otherwise invisible — restore-from-backup detection is off for good (the classic
    # path: the marker is written by a root helper under umask 077, so it lands 600
    # root:root and the uid-1000 container can never read it), every other fingerprint
    # component travels inside the backup and matches, and the only trace is a log line
    # every five minutes.
    reg.metric(
        "curator_restore_marker_unreadable",
        "1 iff the configured restore marker could not be read by the last pass that "
        "needed it (backup-restore detection is blind); 0 if read, not configured, or "
        "not yet looked at.",
        "gauge",
        [({}, 1 if snap.restore_marker_unreadable else 0)],
    )
    # Process-monotonic auth-rejection counter, now a LABELED family: one series per
    # coarse {reason} the increment sites already pass (api_token / metrics_token /
    # mcp_token, the /ext reject codes protocol|auth|origin|duplicate_instance|revoked|
    # unknown_instance|capacity, cors_preflight, and the enroll_* labels). Read from the
    # process-memory singleton — legitimately in-memory (a rejections counter resets on
    # restart like any Prometheus process counter), so it is served even in degraded mode.
    # An empty breakdown (a fresh process that has rejected nothing) emits NO samples,
    # exactly like the other per-label families (`{kind}` / `{to_instance}`): the HELP/TYPE
    # lines are still present, so a scrape never breaks.
    reg.metric(
        "curator_auth_rejections_total",
        "Process-monotonic count of auth rejections across every gated surface, by "
        "coarse reason; resets on restart. Empty (no rejections yet) emits no series.",
        "counter",
        [
            ({"reason": reason}, count)
            for reason, count in sorted(auth_rejections.by_reason().items())
        ],
    )

    return reg.render()


async def metrics(request: Request) -> Response:
    """``GET /metrics`` — the read-only Prometheus scrape (§12).

    Guarded by ``METRICS_TOKEN`` only (never ``EXT_TOKEN``). Serves in degraded mode:
    a failed DB read degrades to defaults and still emits every metric, with
    ``curator_migration_failed=1``.
    """
    require_metrics_token(request)
    settings = request.app.state.settings
    degraded = bool(getattr(request.app.state, "degraded", False))
    db = getattr(request.app.state, "db", None)
    now_ms = int(time.time() * 1000)

    snap: Snapshot | None = None
    if db is not None:
        try:
            snap = await db.read(lambda c: _collect(c, settings.main_instance_id))
        except Exception:  # noqa: BLE001 - a broken/untrusted schema must not 500 /metrics
            logger.exception("metrics: DB collect failed; serving degraded metrics")
            snap = None
    if snap is None:
        # Degraded / no DB: emit every metric at its safe default and flag the failure.
        snap = Snapshot()
        degraded = True

    body = _render(snap, settings, now_ms, degraded)
    return Response(body, media_type=CONTENT_TYPE)
