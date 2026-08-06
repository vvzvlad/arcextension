"""The curator pass orchestration — the nine steps of §7.

Order (§7): (0) server-clock check; (1) the stop gate + the resume_pending latch read;
(2) lease with fencing epoch + a SEPARATE renewal task; (3) snapshot freshness keyed
on the pass's OWN request ids; (4-8) guards / routing / phase A / phase B / dedup /
singleton, each tab isolated; the ``passes`` row is written for every real pass
(even an empty one). Window merge (step 9, §9) runs LAST, after the tab decisions,
so those decide against a stable window picture (Фаза 15).

The ONE mass-action protection is the plan-size threshold (``MAX_ACTIONS_PER_PASS``):
a real pass whose countable plan (phase-A relocations + closes) exceeds it defers the
plan behind a confirming click instead of executing it — see the threshold gate below
for the full design rationale.

Every mutation is a lease-guarded ``Database.write`` (§7): a lost lease raises
:class:`~src.curator.lease.LeaseLost`, which stops the pass at once. WS I/O and the
snapshot wait live OUTSIDE transactions (§4).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from dataclasses import dataclass

from loguru import logger

from src.curator import decide as decidemod
from src.curator import lease, pause, phases
from src.curator.mirror import load_mirror
from src.db.actions import read_pending_closes, read_revoked_relocations
from src.db.settings_store import get_setting, set_setting
from src.ext import protocol
from src.rules import access


def _now_ms() -> int:
    return int(time.time() * 1000)


# The latch and the stop flag live in `settings` under the names pause.py owns
# (the rows MUST share a name across the runner, the API guards and /metrics).
_RESUME_PENDING_KEY = pause.RESUME_PENDING_KEY
_STOPPED_AT_KEY = pause.STOPPED_AT_KEY
# The last observed server-clock step (seconds) is persisted here so /metrics can
# export curator_clock_step_seconds (§12): the clock-step abort happens BEFORE any
# `passes` row is written, so settings is the only durable place to record it.
_CLOCK_STEP_KEY = "curator_clock_step_seconds"

# ``decision`` values on an aggregated `deferred` row — the CAUSE of the deferral (§7).
# ``target_not_ready`` is the §7 one (connection / epoch change / an unreachable home);
# ``dup_same_pass`` is the curator serialising itself, which is a different animal and
# must stay tellable apart in the journal.
_DEFER_UNREADY = "target_not_ready"
_DEFER_SAME_URL = "dup_same_pass"


@dataclass
class Readiness:
    instance_id: str
    conn_epoch: int
    session_id: str | None
    snapshot_id: str


# --- passes-row writers (sync fn) -------------------------------------------
def _insert_pass_started(conn: sqlite3.Connection, pass_id: str, started_at: int) -> None:
    conn.execute(
        "INSERT INTO passes (pass_id, started_at) VALUES (?, ?)", (pass_id, started_at)
    )


def _finalize_pass(conn: sqlite3.Connection, pass_id: str, **cols) -> None:
    conn.execute(
        "UPDATE passes SET finished_at = ?, ok = ?, instances_ready = ?, "
        "tabs_considered = ?, actions_count = ?, error = ? WHERE pass_id = ?",
        (
            cols.get("finished_at"), cols.get("ok"), cols.get("instances_ready"),
            cols.get("tabs_considered"), cols.get("actions_count"), cols.get("error"),
            pass_id,
        ),
    )


# --- step 2 helper: stop check + lease acquire in ONE transaction (§7) -------
def _acquire_unless_stopped(
    conn: sqlite3.Connection, owner: str, now: int, ttl_ms: int, *, honor_stop: bool
):
    """Take the lease unless the curator is stopped. Returns ``(acquired, epoch,
    stopped_since)``.

    ``curator_stopped_at`` and the lease both live in ``settings``, so this is one
    synchronous ``fn(conn)`` inside a single ``BEGIN IMMEDIATE`` — which is what closes
    the window between the step-1 stop read and the acquire (the TOCTOU hole: a stop
    pressed between that read and this write would otherwise be out-epoched by the
    acquire's own bump, and the pass would run through an armed emergency stop). When
    stopped NOTHING is touched (no epoch bump, no owner), and ``stopped_since`` names
    the stop moment.

    ``honor_stop`` is False only for an explicit ``dry_run``: §7 keeps the plan
    readable while stopped. The step-1 check stays where it is — it exits early without
    ever touching the lease; this is the race-safe duplicate for the window after it.
    """
    if honor_stop:
        stopped_since = pause.read_stopped_at(conn)
        if stopped_since is not None:
            return False, 0, stopped_since
    acquired, epoch = lease.acquire(conn, owner, now, ttl_ms)
    return acquired, epoch, None


# --- §12: rules are re-validated on EVERY pass -------------------------------
def _revalidate_rules(conn: sqlite3.Connection, main_instance_id: str) -> int:
    """Re-check every rule's target instance and pattern (§8/§12, one writer ``fn``).

    §12 requires this on every pass, not only at save time: validation at save catches
    a typo, but RETIRING an instance produces exactly the same orphaned rule afterwards
    — silently, and preview cannot show it (it counts pattern matches, not target
    existence). Orphans end up ``invalid=1``, which the existing
    ``curator_rules_invalid`` alert and the editor highlight already surface.

    **``MAIN_INSTANCE_ID`` counts as known even with no ``instances`` row**, exactly as
    the save-time check does (``src.api.rules._validate_instance_or_422``): §12 demands
    ONE rule ("валидируется ... при записи и на каждом проходе"), and a main that has
    never connected is a state the product explicitly models (the
    ``curator_main_instance_never_seen`` metric). Without the exemption a legally saved
    "X -> main" rule is invalidated by the first pass; if it is the only rule,
    ``has_active_rules`` then reports an empty policy, the unruled drain switches off
    and the curator quietly stops doing anything at all — while an alert blames the
    owner's rule.

    Runs BEFORE the mirror is captured, so THIS pass already routes with the corrected
    flags.
    """
    known = access.known_instance_ids(conn) | {main_instance_id}
    return access.revalidate_rules(conn, known)


# --- snapshot readiness (step 3), keyed on the pass's OWN ids ----------------
async def _request_all_snapshots(registry) -> dict:
    """Send a fresh ``snapshot_request`` to every connected instance; return
    ``{instance_id: (conn_state, request_id)}``. The id is the pass's own — a foreign
    (human Cmd+T) snapshot landing mid-pass moves ``snapshot_at`` but NOT this id, so
    readiness (keyed here) is not disturbed (§6/§7)."""
    sent = {}
    for iid, cs in registry.items():
        rid = f"pass-{uuid.uuid4()}"
        cs.pending_snapshot_id = rid
        cs.pending_sent_at = _now_ms()
        try:
            await cs.ws.send_json({"type": protocol.TYPE_SNAPSHOT_REQUEST, "id": rid})
        except Exception:  # noqa: BLE001 - a socket that died mid-send is just not ready
            continue
        sent[iid] = (cs, rid)
    return sent


async def _await_ready(registry, sent: dict, timeout_ms: int) -> dict:
    """Wait until each instance answers ITS pass request id, else drop it (§7).

    An instance is ready iff a snapshot carrying THIS pass's request id was applied
    (``last_applied_snapshot_id == rid``) and the socket is still the same live one.
    Readiness is captured ONCE here; later foreign snapshots cannot revoke it."""
    ready: dict = {}
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for iid, (cs, rid) in sent.items():
            if iid in ready:
                continue
            if registry.get(iid) is not cs:
                continue  # socket superseded/dropped underneath us
            if cs.last_applied_snapshot_id == rid:
                ready[iid] = Readiness(iid, cs.conn_epoch, cs.session_id, rid)
        if len(ready) == len(sent):
            break
        await asyncio.sleep(0.02)
    return ready


async def _snapshot_request_everyone(registry) -> None:
    """Fire-and-forget a snapshot_request to all connected (clock-step recovery, §7:
    the snapshot's client ``ageMs`` rebases the whole mirror past the server step)."""
    await _request_all_snapshots(registry)


# --- the plan (dry_run / resume_pending) ------------------------------------
def _countable(decisions) -> int:
    """The actions the threshold gate counts: phase-A relocations + closes.

    Phase-B completions, abandon bookkeeping, deferred journaling and window merges are
    NOT counted — none of them destroys state the owner has not already approved (see
    the threshold gate in :func:`run_pass`)."""
    return len(decisions.phase_a) + len(decisions.closes)


def _plan(decisions, threshold: int) -> dict:
    relocations = [
        {"url": d.tab.url, "from": d.tab.instance_id, "to": d.home}
        for d in decisions.phase_a
    ]
    phase_b = [
        {"url": d.reloc.url, "from": d.reloc.instance_from, "to": d.reloc.instance_to}
        for d in decisions.phase_b
    ]
    closures = [
        {"url": d.tab.url, "instance": d.tab.instance_id, "kind": d.kind}
        for d in decisions.closes
    ]
    return {
        "relocations": len(relocations),
        "phase_b_completions": len(phase_b),
        "closures": len(closures),
        "deferred": dict(decisions.deferred),
        # Why part of `deferred` happened: an identical (target, url) pair was already
        # scheduled for phase A this pass, so the second tab waits (§7).
        "deferred_same_url": dict(decisions.deferred_same_url),
        # The countable total vs the brake it is compared against — carried in the plan
        # (dry_run and latch alike) so the UI can say "34 actions > 20" without
        # re-deriving which kinds count.
        "total": _countable(decisions),
        "threshold": threshold,
        "relocation_examples": relocations[:20],
        "closure_examples": closures[:20],
        "phase_b_examples": phase_b[:20],
    }


# --- isolation helper (§7: each tab in its own try/except) -------------------
async def _isolated(coro) -> None:
    """Run one decision's execution; isolate ordinary failures but let a lost lease
    propagate so the whole pass stops (§7)."""
    try:
        await coro
    except lease.LeaseLost:
        raise
    except Exception:  # noqa: BLE001 - one bad tab must not sink the pass
        logger.exception("curator: isolated decision failed")


# --- the pass ----------------------------------------------------------------
async def run_pass(
    db, registry, settings, *,
    dry_run: bool = False,
    confirm_pending: bool = False,
    clock_guard=None,
    now: int | None = None,
) -> dict:
    """Run one curator pass. Returns a status/plan/counts dict (for the endpoint)."""
    now = now if now is not None else _now_ms()

    # --- step 0: server-clock check (§7) — the FIRST thing, before the lease ---
    if clock_guard is not None:
        skew = clock_guard.check()
        if clock_guard.exceeds(skew):
            await _snapshot_request_everyone(registry)
            # Persist the step for curator_clock_step_seconds (§12). Best-effort: a
            # write fault here must not stop the abort/recovery path.
            try:
                await db.write(lambda c: set_setting(c, _CLOCK_STEP_KEY, str(skew)))
            except Exception:  # noqa: BLE001 - observability write must never break the abort
                logger.exception("curator: failed to persist clock step")
            logger.warning("curator: server clock step {:.1f}s — pass aborted", skew)
            return {"status": "clock_step", "clock_step_seconds": skew}

    idle_ms = settings.idle_minutes * 60_000

    # --- step 1: the stop gate + the resume_pending latch read (§7) ----------
    # ``dry_run`` is never muted by a stop — looking at the plan is exactly why the
    # stop is pressed (§7) — so the gate is skipped for it. Everything else,
    # ``confirm_pending`` included, loses to a LIVE stop: if the owner saw the deferred
    # plan and hit "stop", the confirm must not run a full pass through an active
    # emergency stop.
    if not dry_run:
        stopped_at = get_setting_int(await _read_setting(db, _STOPPED_AT_KEY))
        if stopped_at is not None:
            return {"status": "stopped", "since": stopped_at}
        resume_armed = bool(await _read_setting(db, _RESUME_PENDING_KEY))
        # A ``confirm_pending`` confirms an ARMED plan. With no latch there is nothing
        # to confirm, and honouring the flag anyway would let a client that sends it out
        # of habit pre-approve a plan nobody has seen: an over-threshold plan computed
        # below would execute instead of latching. So it degrades to an ordinary pass —
        # which still latches if THIS pass's plan turns out over-threshold.
        if confirm_pending and not resume_armed:
            logger.info("curator: confirm_pending with no armed latch — running the normal gate")
            confirm_pending = False
        # An armed latch does NOT early-return: the pass always proceeds to decide().
        # That is what makes the latch self-refreshing (every pass overwrites it with
        # the CURRENT plan, so the shown plan is at most one interval stale) and
        # self-clearing (a plan that shrank back under the threshold simply executes
        # and drops the latch, no click needed) — see the threshold gate below.

    # --- step 2: acquire the lease with a fencing epoch (§7) ------------------
    owner = f"pass-{uuid.uuid4()}"
    # The stop flag is re-read INSIDE the acquiring transaction. Step 1's check alone is
    # a TOCTOU hole: a stop pressed between that read and this write bumps the epoch to
    # E+1, `acquire` immediately bumps it to E+2, and the pass then owns the freshest
    # epoch — every guarded write passes, ``curator_stopped_at`` is never consulted
    # again, and the owner who hit the emergency stop watches the curator close tabs
    # for minutes (§7 "этого достаточно, чтобы остановить уже идущий проход"). Both
    # values live in ``settings``, so the check costs one extra SELECT in the same
    # ``fn(conn)`` — no await, no second transaction, no race left.
    acquired, epoch, stopped_since = await db.write(
        lambda c: _acquire_unless_stopped(
            c, owner, now, settings.lease_ttl_ms, honor_stop=not dry_run
        )
    )
    if stopped_since is not None:
        return {"status": "stopped", "since": stopped_since}
    if not acquired:
        return {"status": "lease_unavailable"}
    pass_start = now
    pass_id = f"pass-{uuid.uuid4()}"

    renew_task = asyncio.create_task(
        _renew_loop(db, owner, epoch, settings.lease_ttl_ms)
    )
    ctx = None
    considered = 0
    ready: dict = {}
    error: str | None = None
    deferred_plan: dict | None = None
    try:
        if not dry_run:
            await db.write(lease.guarded(epoch, lambda c: _insert_pass_started(c, pass_id, pass_start)))

        # --- step 3: freshness — snapshot_request keyed on our OWN ids -------
        sent = await _request_all_snapshots(registry)
        ready = await _await_ready(registry, sent, settings.snapshot_timeout_ms)
        ready_ids = set(ready)

        # §12: re-validate the rules against the instances the DB now knows, under the
        # lease guard, BEFORE the mirror is frozen so this pass already routes with the
        # corrected flags.
        #
        # Runs for a dry_run TOO. "dry_run writes nothing" is a promise about the
        # ACTIONS journal (§12) — the plan must cost no closures and no `passes` row —
        # and `rules.invalid` is neither: it is the policy's own health, which §12 says
        # every pass re-derives. Skipping it here would make the plan the owner CONFIRMS
        # behind the threshold latch disagree with the pass that executes it: the plan
        # would be routed on stale flags, the confirming pass would revalidate first and
        # route differently, and "confirmed" would stop meaning "this is what will
        # happen" for exactly the rules §12 wants re-checked.
        await db.write(
            lease.guarded(
                epoch, lambda c: _revalidate_rules(c, settings.main_instance_id)
            )
        )

        # --- retire relocations of REVOKED instances (issue #35 §5) ----------
        # The one lease-guarded region that does NOT depend on ready_ids (§5: "единственный
        # участок под гардом аренды, не зависящий от ready_ids"), placed AFTER
        # _revalidate_rules and BEFORE the mirror is frozen — which is what makes it
        # race-free against phase B: a relocation marked `abandoned` here is no longer
        # `status='done'`, so the mirror captured just below excludes it from
        # `live_relocations`, and `decide`/phase B never see it as a phase-B candidate.
        # phases.py warns a retire racing phase B would journal a relocation both `closed`
        # AND `abandoned`; running before the capture — with no other pass able to run,
        # the lease slot — removes that race entirely. Reuses the §33 action-status helper
        # (`mark_action_abandoned`), not a parallel mechanism. Skipped on dry_run (a
        # dry_run writes no actions, exactly like reconcile). Per row isolated; one failed
        # retire must not abort the pass.
        if not dry_run:
            for reloc_id in await db.read(read_revoked_relocations):
                await _isolated(_retire_revoked_relocation(db, epoch, reloc_id))

        # --- capture the frozen mirror; all decisions run against it ---------
        mirror = await db.read(load_mirror)
        ctx = phases.PassCtx(
            db=db, registry=registry, settings=settings, pass_id=pass_id,
            epoch=epoch, now=now, idle_ms=idle_ms, mirror=mirror, struck=set(),
            ready=ready,
        )

        # --- reconcile: resolve prior-pass PENDING closes EARLY, before decide
        # (§7, WARNING-1). Each pending *_close (a close whose completion write was
        # fenced by a lost lease) is resolved against this fresh mirror: → done when
        # the source is gone (at-least-once journal), → abandoned when it is still
        # present (decide re-issues it). Per row isolated; every write lease-guarded.
        # Skipped on dry_run (a dry_run writes no actions). Reconcile does not mutate
        # the frozen mirror, so decide below still decides against the same picture;
        # a pending relocate_close is already excluded from live_relocations, so a
        # relocation is never both reconciled AND phase-B'd in the same pass.
        # A row whose source instance did NOT answer THIS pass's snapshot is left
        # alone by ``run_reconcile`` — see its readiness gate. A row younger than one
        # ``cmd_timeout_ms`` is skipped by ``read_pending_closes`` itself (#48): the
        # synchronous relocate_tab verb writes its pending close BEFORE sending the
        # source close, so its row must not be reconciled while that verb is still
        # awaiting the response.
        if not dry_run:
            for pending_row in await db.read(
                lambda c: read_pending_closes(
                    c, now=now, cmd_timeout_ms=settings.cmd_timeout_ms
                )
            ):
                await _isolated(phases.run_reconcile(ctx, pending_row))

        decisions = decidemod.decide(
            mirror, ready_ids,
            now=now, idle_ms=idle_ms, main_instance_id=settings.main_instance_id,
        )
        considered = decisions.considered

        if dry_run:
            return {"status": "dry_run",
                    "plan": _plan(decisions, settings.max_actions_per_pass)}

        # --- the plan-size threshold gate (§7) — the ONE mass-action brake ---
        # Cause-agnostic by design: a restored backup, a clean DB whose whole fleet
        # turns eligible at once, a lowered IDLE_MINUTES, a fat-fingered rule — every
        # such event does its damage as "too many actions in one pass", so the SIZE of
        # the plan is what is gated, not a guess at its cause. When the countable plan
        # (phase-A relocations + closes) exceeds MAX_ACTIONS_PER_PASS, the pass does
        # not execute it: it stores the CURRENT plan in the resume_pending latch and
        # waits for one confirming click (run_pass{confirm_pending} / the resume
        # button).
        #
        # The latch is a fresh snapshot, not a one-shot: every subsequent pass still
        # runs decide() and OVERWRITES it, so the plan the owner sees is at most one
        # pass-interval stale — and a plan that shrinks back under the threshold simply
        # executes on the normal path below and clears the latch (auto-unlatch, no
        # click needed). The click therefore executes the plan recomputed AT CLICK
        # TIME, not the possibly stale one that first armed the latch.
        #
        # Phase-B completions are EXEMPT — excluded from the count AND still executed
        # while the latch is armed. They are the second half of already-approved work
        # (the copy is already open in the target); freezing them would keep duplicate
        # tabs open for as long as the owner takes to click, and phases.run_phase_b's
        # own target verification already protects against stale relocate rows. The
        # abandon bookkeeping runs for the same reason: it retires rows decide() has
        # already proven stale, destroying nothing.
        countable = _countable(decisions)
        if countable > settings.max_actions_per_pass and not confirm_pending:
            deferred_plan = _plan(decisions, settings.max_actions_per_pass)
            # Say it out loud: arming (unarmed -> armed) is an event the operator
            # wants in the log, a refresh is routine chatter. Step 1 already read
            # the latch, so `resume_armed` tells the two apart.
            if resume_armed:
                logger.debug(
                    "curator: threshold latch refreshed: plan of {} countable actions"
                    " > threshold {}", countable, settings.max_actions_per_pass,
                )
            else:
                logger.info(
                    "curator: threshold latch armed: plan of {} countable actions"
                    " > threshold {} — waiting for a confirming click",
                    countable, settings.max_actions_per_pass,
                )
            for ab in decisions.abandon:
                await _isolated(_abandon(ctx, ab))
            for dec in decisions.phase_b:
                await _isolated(phases.run_phase_b(ctx, dec))
            # SKIPPED: deferred-row journaling, phase A, closes, window merges — the
            # countable work waits for the click; journaling a deferral that is itself
            # deferred would only muddy curator_deferred_total.
            plan_value = _resume_pending_value(pass_start, deferred_plan)
            await db.write(
                lease.guarded(
                    epoch, lambda c: set_setting(c, _RESUME_PENDING_KEY, plan_value)
                )
            )
        else:
            # --- steps 4-8: execute (each decision isolated) -----------------
            # Abandon stale relocate rows (source moved/gone); their source tabs, if
            # any, were already re-routed in the SAME decide() call (not "owned").
            for ab in decisions.abandon:
                await _isolated(_abandon(ctx, ab))
            # Aggregated deferred rows: one per (pass_id, instance_to, cause) (§7 step
            # 6). §7 defines `deferred` as "вызвано соединением, сменой эпохи или
            # слиянием окон", and the same-url hold-back (a tab waiting for the copy
            # this pass is opening) is a DIFFERENT thing wearing the same status.
            # Splitting the rows by ``decision`` keeps curator_deferred_total exactly
            # as it was (metrics sums every deferred row per instance_to) while making
            # the cause answerable from the journal and separable by a later metrics
            # change — see the report.
            for to_instance, count in decisions.deferred.items():
                same_url = decisions.deferred_same_url.get(to_instance, 0)
                if count - same_url > 0:
                    await _isolated(
                        _write_deferred(ctx, to_instance, count - same_url, _DEFER_UNREADY)
                    )
                if same_url > 0:
                    await _isolated(
                        _write_deferred(ctx, to_instance, same_url, _DEFER_SAME_URL)
                    )
            for dec in decisions.phase_b:
                await _isolated(phases.run_phase_b(ctx, dec))
            for dec in decisions.phase_a:
                await _isolated(phases.run_phase_a(ctx, dec))
            for dec in decisions.closes:
                await _isolated(phases.run_close(ctx, dec))

            # --- step 9: window merge (§9), LAST — the tab decisions (4-8) ran
            # against the STABLE window picture; only now do the instance's windows
            # collapse into one. Planned purely from the same frozen mirror; each
            # instance isolated so one bad merge cannot sink the pass; every write
            # lease-guarded (§7/§9).
            # main is skipped unconditionally (#41): its window layout is the owner's,
            # and a merge is not undoable.
            for merge in decidemod.decide_window_merges(
                mirror, ready_ids, now=now, idle_ms=idle_ms,
                main_instance_id=settings.main_instance_id,
            ):
                await _isolated(phases.run_window_merge(ctx, merge))

            # A pass that executed its (under-threshold or confirmed) plan clears the
            # latch: whatever was deferred either just ran or no longer exists (§7).
            # ONLY when the pass had evidence, though: the plan is computed FROM the
            # ready instances, so an empty ready set produces an empty plan by
            # construction and proves nothing about the armed plan having shrunk.
            # Clearing on it would make the latch (and curator_resume_pending) flap
            # with fleet connectivity — a sleeping laptop erases a 500-action plan,
            # the next connected pass re-arms it. So the latch outlives passes that
            # saw nothing. The confirm path shares this guard deliberately: a click
            # that ran against zero ready instances executed nothing and its plan is
            # still pending — consistent with the resume_now docstring ("a pass that
            # never got to run leaves the latch armed").
            if ready_ids:
                await db.write(lease.guarded(epoch, _clear_deferred_plan))
    except lease.LeaseLost:
        error = "lease_lost"
        logger.info("curator: lease lost mid-pass {}; stopping", pass_id)
    except Exception as exc:  # noqa: BLE001 - record the fault on the passes row
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("curator: pass {} failed", pass_id)
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass
        # Hand the slot back. Keyed on the OWNER, not the epoch: a pass fenced by a stop
        # MUST still release (else the slot is hostage until the TTL), while a pass whose
        # lease was taken over after expiry must NOT (someone else owns it now). This
        # release is what keeps "one pass at a time" true — the slot stays taken for as
        # long as this pass might still be talking to a browser.
        try:
            await db.write(lambda c: lease.release(c, owner))
        except Exception:  # noqa: BLE001
            logger.exception("curator: lease release failed for {}", pass_id)

    if dry_run:
        # (Only reached if a dry_run hit LeaseLost/other error before returning.)
        return {"status": "error" if error else "dry_run", "error": error}

    actions_count = ctx.actions_count if ctx is not None else 0
    instances_ready = len(ready)
    # ok=0 when no instance was ready (§12: a pass that found nobody is NOT healthy)
    # or the pass errored; else ok=1.
    ok = 1 if (error is None and instances_ready > 0) else 0
    try:
        await db.write(
            lambda c: _finalize_pass(
                c, pass_id, finished_at=_now_ms(), ok=ok,
                instances_ready=instances_ready, tabs_considered=considered,
                actions_count=actions_count, error=error,
            )
        )
    except Exception:  # noqa: BLE001 - a passes-row finalize fault must not mask result
        logger.exception("curator: finalize passes row failed for {}", pass_id)

    # An over-threshold pass that latched its plan DID run (it kept the exempt phase-B
    # work moving and refreshed the latch), so it goes through the normal finalize
    # above — but its answer to the caller is the deferred plan, not "ok".
    if deferred_plan is not None and error is None:
        return {
            "status": "resume_pending",
            "plan": deferred_plan,
            "pass_id": pass_id,
            "instances_ready": instances_ready,
            "tabs_considered": considered,
            "actions_count": actions_count,
        }

    return {
        "status": "ok" if ok else ("error" if error else "no_ready_instances"),
        "pass_id": pass_id,
        "instances_ready": instances_ready,
        "tabs_considered": considered,
        "actions_count": actions_count,
        "error": error,
    }


# --- sub-executions ----------------------------------------------------------
async def _retire_revoked_relocation(db, epoch: int, reloc_id: int) -> None:
    """Mark ONE live relocate row of a revoked instance ``abandoned`` (issue #35 §5),
    under the lease guard. Reuses the §33 helper; a lost lease propagates (stopping the
    pass), a per-row failure is isolated by :func:`_isolated`."""
    from src.db.actions import mark_action_abandoned

    await db.write(lease.guarded(epoch, lambda c: mark_action_abandoned(c, reloc_id)))


async def _abandon(ctx, ab) -> None:
    from src.db.actions import mark_action_abandoned

    await ctx.db.write(lease.guarded(ctx.epoch, lambda c: mark_action_abandoned(c, ab.reloc_id)))


async def _write_deferred(ctx, to_instance: str, count: int, decision: str) -> None:
    """One aggregated ``deferred`` row for (pass, target, cause).

    ``reason`` stays the bare count — ``/metrics`` parses it as an int for
    ``curator_deferred_total`` — so the cause goes in ``decision``, the column §4 keeps
    for "the BASIS of the decision".
    """
    from src.db.actions import insert_action

    def _w(conn):
        insert_action(
            conn, ts=ctx.now, kind="relocate", status="deferred", initiator="curator",
            pass_id=ctx.pass_id, instance_to=to_instance, reason=str(count),
            decision=decision,
        )

    await ctx.db.write(lease.guarded(ctx.epoch, _w))
    ctx.actions_count += 1


def _clear_deferred_plan(conn: sqlite3.Connection) -> None:
    """Drop the threshold latch (a writer ``fn``): a pass that executed its plan —
    under-threshold or confirmed — leaves nothing pending. Clearing on EVERY executing
    pass (not only a confirming one) is the auto-unlatch half of the design: a plan
    that shrank back under the threshold runs normally and the stale latch goes with
    it, no click needed. The caller guards this on a non-empty ready set — an
    evidence-free pass must not clear (see the execute path in :func:`run_pass`)."""
    set_setting(conn, _RESUME_PENDING_KEY, "")


def _resume_pending_value(since: int, plan: dict) -> str:
    import json

    return json.dumps({"since": since, "plan": plan})


# --- lease renewal (SEPARATE task, cancelled by the same finally) -----------
async def _renew_loop(db, owner: str, epoch: int, ttl_ms: int) -> None:
    """Extend the lease every ``ttl_ms/3`` until cancelled or the lease is lost.

    Hardened (WARNING 1, Фаза-8 sub-path): a TRANSIENT write fault (degraded DB, a
    disk blip) must NOT silently stop renewal — a stopped loop lets the lease expire,
    another pass then bumps the epoch, and this still-running pass loses its lease
    mid-flight. So a transient error is logged and RETRIED next interval; the loop
    stops ONLY on a definitive lost lease (``renew`` returns ``False``) or on
    cancellation (the pass's ``finally`` cancels it).
    """
    interval = (ttl_ms / 3) / 1000.0
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await db.write(lambda c: lease.renew(c, owner, epoch, _now_ms(), ttl_ms))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - transient write fault: retry, do not stop
                logger.exception("curator: lease renewal error (epoch {}); retrying", epoch)
                continue
            if not ok:
                logger.info("curator: lease renewal lost (epoch {})", epoch)
                return
    except asyncio.CancelledError:
        raise


# --- small settings readers --------------------------------------------------
async def _read_setting(db, key: str):
    return await db.read(lambda c: get_setting(c, key))


def get_setting_int(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None
