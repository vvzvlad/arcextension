"""The curator pass orchestration — the nine steps of §7.

Order (§7): (0) server-clock check; (1) pause / resume_pending / continuity gate;
(2) lease with fencing epoch + a SEPARATE renewal task; (3) snapshot freshness keyed
on the pass's OWN request ids; (4-8) guards / routing / phase A / phase B / dedup /
singleton, each tab isolated; the ``passes`` row is written for every real pass
(even an empty one). Window merge (step 9, §9) is a SEPARATE phase (Фаза 15) and is
left as a documented hook.

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

from src.curator import clock as clockmod
from src.curator import decide as decidemod
from src.curator import lease, phases
from src.curator.mirror import load_mirror
from src.db.settings_store import get_setting, set_setting
from src.ext import protocol


def _now_ms() -> int:
    return int(time.time() * 1000)


_RESUME_PENDING_KEY = "resume_pending"
_PAUSE_UNTIL_KEY = "pause_until"
# The last observed server-clock step (seconds) is persisted here so /metrics can
# export curator_clock_step_seconds (§12): the clock-step abort happens BEFORE any
# `passes` row is written, so settings is the only durable place to record it.
_CLOCK_STEP_KEY = "curator_clock_step_seconds"


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
def _plan(decisions) -> dict:
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

    # --- step 1: pause / resume_pending / continuity gate (§7) ---------------
    effective_dry_run = dry_run
    armed_after_break = False
    # TODO(Фаза 16): SUGGESTION 7 — ``confirm_pending`` (a resume click) currently
    # skips this whole gate, so it also bypasses an ACTIVE ``pause_until``. That is
    # harmless today (no endpoint arms a pause), but once the Фаза 16 pause endpoints
    # land, a confirm must still honour a live pause. Re-check ``pause_until`` on the
    # confirm_pending path then.
    if not dry_run and not confirm_pending:
        pause_until = get_setting_int(await _read_setting(db, _PAUSE_UNTIL_KEY))
        if pause_until is not None and pause_until > now:
            return {"status": "paused", "until": pause_until}
        if await _read_setting(db, _RESUME_PENDING_KEY):
            return {"status": "resume_pending"}
        current_fp = await db.read(
            lambda c: clockmod.current_fingerprint(
                c, idle_minutes=settings.idle_minutes,
                main_instance_id=settings.main_instance_id,
            )
        )
        stored_fp = await db.read(clockmod.read_stored_fingerprint)
        if clockmod.is_continuity_break(stored_fp, current_fp):
            # First pass after a continuity break: compute a dry-run plan and arm
            # resume_pending; the owner confirms with run_pass{confirm_pending} (§7).
            effective_dry_run = True
            armed_after_break = True

    # --- step 2: acquire the lease with a fencing epoch (§7) ------------------
    owner = f"pass-{uuid.uuid4()}"
    acquired, epoch = await db.write(
        lambda c: lease.acquire(c, owner, now, settings.lease_ttl_ms)
    )
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
    try:
        if not effective_dry_run:
            await db.write(lease.guarded(epoch, lambda c: _insert_pass_started(c, pass_id, pass_start)))

        # --- step 3: freshness — snapshot_request keyed on our OWN ids -------
        sent = await _request_all_snapshots(registry)
        ready = await _await_ready(registry, sent, settings.snapshot_timeout_ms)
        ready_ids = set(ready)

        # --- capture the frozen mirror; all decisions run against it ---------
        mirror = await db.read(load_mirror)
        decisions = decidemod.decide(
            mirror, ready_ids,
            now=now, idle_ms=idle_ms, main_instance_id=settings.main_instance_id,
        )
        considered = decisions.considered

        if effective_dry_run:
            plan = _plan(decisions)
            if armed_after_break:
                await db.write(
                    lease.guarded(
                        epoch,
                        lambda c: set_setting(
                            c, _RESUME_PENDING_KEY,
                            _resume_pending_value(pass_start, plan),
                        ),
                    )
                )
                return {"status": "resume_pending", "plan": plan}
            return {"status": "dry_run", "plan": plan}

        # --- steps 4-8: execute (each decision isolated) ---------------------
        ctx = phases.PassCtx(
            db=db, registry=registry, settings=settings, pass_id=pass_id,
            epoch=epoch, now=now, idle_ms=idle_ms, mirror=mirror, struck=set(),
            ready=ready,
        )
        # Abandon stale relocate rows (source moved/gone); their source tabs, if any,
        # were already re-routed in the SAME decide() call (they are not "owned").
        for ab in decisions.abandon:
            await _isolated(_abandon(ctx, ab))
        # Aggregated deferred rows: one per (pass_id, instance_to) (§7 step 6).
        for to_instance, count in decisions.deferred.items():
            await _isolated(_write_deferred(ctx, to_instance, count))
        for dec in decisions.phase_b:
            await _isolated(phases.run_phase_b(ctx, dec))
        for dec in decisions.phase_a:
            await _isolated(phases.run_phase_a(ctx, dec))
        for dec in decisions.closes:
            await _isolated(phases.run_close(ctx, dec))

        # step 9 — window merge (§9) is Фаза 15; intentionally a no-op hook here.

        # A real pass with established continuity refreshes the fingerprint and
        # clears any resume_pending it was confirming (§7).
        await db.write(lease.guarded(epoch, lambda c: _finish_continuity(c, settings)))
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
        # Release the lease (no-op if the epoch moved — never clear someone else's).
        try:
            await db.write(lambda c: lease.release(c, owner, epoch))
        except Exception:  # noqa: BLE001
            logger.exception("curator: lease release failed for {}", pass_id)

    if effective_dry_run:
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

    return {
        "status": "ok" if ok else ("error" if error else "no_ready_instances"),
        "pass_id": pass_id,
        "instances_ready": instances_ready,
        "tabs_considered": considered,
        "actions_count": actions_count,
        "error": error,
    }


# --- sub-executions ----------------------------------------------------------
async def _abandon(ctx, ab) -> None:
    from src.db.actions import mark_action_abandoned

    await ctx.db.write(lease.guarded(ctx.epoch, lambda c: mark_action_abandoned(c, ab.reloc_id)))


async def _write_deferred(ctx, to_instance: str, count: int) -> None:
    from src.db.actions import insert_action

    def _w(conn):
        insert_action(
            conn, ts=ctx.now, kind="relocate", status="deferred", initiator="curator",
            pass_id=ctx.pass_id, instance_to=to_instance, reason=str(count),
        )

    await ctx.db.write(lease.guarded(ctx.epoch, _w))
    ctx.actions_count += 1


def _finish_continuity(conn: sqlite3.Connection, settings) -> None:
    """Refresh the continuity fingerprint and clear resume_pending (a real pass ran)."""
    fp = clockmod.current_fingerprint(
        conn, idle_minutes=settings.idle_minutes,
        main_instance_id=settings.main_instance_id,
    )
    clockmod.store_fingerprint(conn, fp)
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
