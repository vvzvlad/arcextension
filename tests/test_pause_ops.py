"""Фаза 16 — pause WRITE side unit tests (§7 «Пауза»).

Covers the shared :mod:`src.curator.pause` primitives and the runner's pause lifecycle
directly (no HTTP), so each mechanic reddens if its guard drops:

* the TTL shift is the ACTUAL elapsed time, not the requested pause (§7),
* the shift is applied EXACTLY ONCE per pause (double-shift guard),
* ``pause_started_at`` survives an extend re-press,
* a pause is ALWAYS finite (the cap),
* the epoch bump stops an in-flight pass at its next guarded write,
* after a timeout expiry the pass DEFERS (resume_pending) instead of auto-running,
* a ``confirm_pending`` click still loses to a live pause (SUGGESTION 7).
"""

from types import SimpleNamespace

import pytest

from src.curator import lease
from src.curator import pause as pause_ops
from src.curator import runner
from src.db.access import Database
from src.db.settings_store import get_setting
from src.ext.registry import Registry


def _settings(**over):
    s = dict(
        idle_minutes=60, pass_interval_min=5, cmd_timeout_ms=1000,
        snapshot_timeout_ms=50, lease_ttl_ms=600_000, state_fresh_ms=3000,
        quarantine_ttl_min=1440, main_instance_id="main", pause_default_min=60,
    )
    s.update(over)
    return SimpleNamespace(**s)


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def _add_exemption(db, iid, url, until):
    await db.write(
        lambda c: c.execute(
            "INSERT INTO exemptions (instance_id, url, until, reason) VALUES (?,?,?,?)",
            (iid, url, until, "restore"),
        )
    )


async def _add_quarantine(db, iid, url, until):
    await db.write(
        lambda c: c.execute(
            "INSERT INTO quarantine (instance_id, url, strikes, until, reason) "
            "VALUES (?,?,?,?,?)",
            (iid, url, 1, until, "flap"),
        )
    )


def _int_setting(db_val):
    return None if db_val in (None, "") else int(db_val)


# --- pause_started_at not overwritten on an extend re-press ------------------
async def test_pause_started_at_survives_extend(tmp_path):
    db = await _make_db(tmp_path)
    await db.write(lambda c: pause_ops.pause(c, now=1_000, minutes=10))
    started1 = await db.read(lambda c: get_setting(c, pause_ops.PAUSE_STARTED_AT_KEY))
    until1 = await db.read(pause_ops.read_pause_until)

    # Re-press 4s later, longer window: the START must NOT move (else the TTL shift
    # under-counts the real duration, §7), but the deadline extends.
    until2 = await db.write(lambda c: pause_ops.pause(c, now=5_000, minutes=60))
    started2 = await db.read(lambda c: get_setting(c, pause_ops.PAUSE_STARTED_AT_KEY))
    assert started2 == started1 == "1000"
    assert until2 == 5_000 + 60 * 60_000 and until2 > until1


# --- a pause is ALWAYS finite (the cap) -------------------------------------
async def test_pause_is_always_finite(tmp_path):
    db = await _make_db(tmp_path)
    until = await db.write(lambda c: pause_ops.pause(c, now=0, minutes=10**9))
    # Clamped to the finite ceiling, never the absurd requested value.
    assert until == pause_ops.PAUSE_MAX_MIN * 60_000
    # A non-positive / non-int request also stays finite and non-zero.
    assert pause_ops.clamp_minutes(0, 60) == 1
    assert pause_ops.clamp_minutes(-5, 60) == 1
    assert pause_ops.clamp_minutes(None, 60) == 60
    assert pause_ops.clamp_minutes("nope", 60) == 60


# --- resume shifts TTL by the ACTUAL elapsed, not the requested hour --------
async def test_resume_shift_is_actual_elapsed(tmp_path):
    db = await _make_db(tmp_path)
    now0 = 1_000_000
    until = await db.write(lambda c: pause_ops.pause(c, now=now0, minutes=60))  # 1h
    assert until == now0 + 3_600_000

    # A 2h protection issued AT pause start (until > started → shiftable) and a stale
    # one that expired BEFORE the pause started (until <= started → left alone, §7).
    prot = now0 + 7_200_000
    stale = now0 - 1
    await _add_exemption(db, "main", "https://prot", prot)
    await _add_exemption(db, "main", "https://stale", stale)
    await _add_quarantine(db, "main", "https://q", prot)

    # Resume after only 5 minutes.
    now1 = now0 + 5 * 60_000
    shift = await db.write(lambda c: pause_ops.resume(c, now=now1))
    assert shift == 5 * 60_000  # ACTUAL elapsed, not the requested hour

    ex = {r[0]: r[1] for r in await db.read(
        lambda c: c.execute("SELECT url, until FROM exemptions").fetchall())}
    q = {r[0]: r[1] for r in await db.read(
        lambda c: c.execute("SELECT url, until FROM quarantine").fetchall())}
    # Shifted by exactly the 5 minutes — NOT the full hour.
    assert ex["https://prot"] == prot + 5 * 60_000
    assert ex["https://prot"] != prot + 3_600_000
    assert q["https://q"] == prot + 5 * 60_000
    # The already-expired protection is untouched.
    assert ex["https://stale"] == stale

    # Pause fully cleared (deadline, start, latch).
    assert await db.read(pause_ops.read_pause_until) is None
    assert _int_setting(
        await db.read(lambda c: get_setting(c, pause_ops.PAUSE_STARTED_AT_KEY))
    ) is None
    assert not await db.read(lambda c: get_setting(c, pause_ops.RESUME_PENDING_KEY))


# --- the shift is applied EXACTLY ONCE per pause (double-shift guard) --------
async def test_shift_applied_exactly_once(tmp_path):
    db = await _make_db(tmp_path)
    now0 = 2_000_000
    await db.write(lambda c: pause_ops.pause(c, now=now0, minutes=60))
    prot = now0 + 7_200_000
    await _add_exemption(db, "main", "https://p", prot)

    now1 = now0 + 10 * 60_000
    first = await db.write(lambda c: pause_ops.apply_resume_shift(c, now=now1))
    assert first == 10 * 60_000
    after1 = await db.read(
        lambda c: c.execute("SELECT until FROM exemptions").fetchone()[0])
    assert after1 == prot + 10 * 60_000

    # A SECOND call (a repeated read/confirm) must shift NOTHING — started is cleared.
    second = await db.write(lambda c: pause_ops.apply_resume_shift(c, now=now1 + 999))
    assert second == 0
    after2 = await db.read(
        lambda c: c.execute("SELECT until FROM exemptions").fetchone()[0])
    assert after2 == after1


# --- a timeout expiry shifts by the FULL pause duration ---------------------
async def test_timeout_expiry_shift_is_full_duration(tmp_path):
    db = await _make_db(tmp_path)
    now0 = 3_000_000
    until = await db.write(lambda c: pause_ops.pause(c, now=now0, minutes=60))
    prot = now0 + 7_200_000
    await _add_exemption(db, "main", "https://p", prot)

    # Observed AFTER the deadline (min(now, pause_until) = pause_until) → full hour.
    shift = await db.write(lambda c: pause_ops.apply_resume_shift(c, now=until + 5_000))
    assert shift == 3_600_000
    after = await db.read(
        lambda c: c.execute("SELECT until FROM exemptions").fetchone()[0])
    assert after == prot + 3_600_000


# --- the epoch bump stops an IN-FLIGHT pass at its next guarded write --------
async def test_pause_fences_in_flight_pass(tmp_path):
    db = await _make_db(tmp_path)
    # A pass is running: it holds the lease at epoch `e`.
    acquired, e = await db.write(lambda c: lease.acquire(c, "pass-1", 0, 600_000))
    assert acquired
    # Pressing pause mid-pass bumps the epoch.
    await db.write(lambda c: pause_ops.pause(c, now=1_000, minutes=30))
    # The in-flight pass's NEXT guarded write now changes zero rows → LeaseLost (§7).
    with pytest.raises(lease.LeaseLost):
        await db.write(lease.guarded(e, lambda c: c.execute(
            "INSERT INTO settings (key, value) VALUES ('x','y')")))


# --- after a timeout expiry the pass DEFERS (resume_pending), then a confirm runs --
async def test_after_expiry_defers_then_confirm_runs(tmp_path):
    db = await _make_db(tmp_path)
    settings = _settings()
    reg = Registry()
    now0 = 10_000_000
    pause_until = await db.write(lambda c: pause_ops.pause(c, now=now0, minutes=1))

    # A scheduled pass AFTER the deadline must NOT auto-run — it arms resume_pending.
    res = await runner.run_pass(db, reg, settings, now=pause_until + 60_000)
    assert res["status"] == "resume_pending"
    assert await db.read(lambda c: get_setting(c, pause_ops.RESUME_PENDING_KEY))
    # Still deferred on the next scheduled pass (latch, not intent).
    res2 = await runner.run_pass(db, reg, settings, now=pause_until + 120_000)
    assert res2["status"] == "resume_pending"

    # The confirm click runs the real pass; the latch + pause start are cleared.
    res3 = await runner.run_pass(
        db, reg, settings, confirm_pending=True, now=pause_until + 130_000
    )
    assert res3["status"] in ("ok", "no_ready_instances")
    assert not await db.read(lambda c: get_setting(c, pause_ops.RESUME_PENDING_KEY))
    assert _int_setting(
        await db.read(lambda c: get_setting(c, pause_ops.PAUSE_STARTED_AT_KEY))
    ) is None


# --- a confirm_pending click still LOSES to a live pause (SUGGESTION 7) ------
async def test_confirm_pending_refuses_under_active_pause(tmp_path):
    db = await _make_db(tmp_path)
    settings = _settings()
    reg = Registry()
    now0 = 20_000_000
    await db.write(lambda c: pause_ops.pause(c, now=now0, minutes=60))
    # A re-armed pause wins even over a confirm: the emergency stop is not bypassable.
    res = await runner.run_pass(
        db, reg, settings, confirm_pending=True, now=now0 + 1_000
    )
    assert res["status"] == "paused" and res["until"] > now0


# --- a pause armed BETWEEN the step-1 read and the step-2 acquire still wins --
class _PauseRacer:
    """A ``Database`` proxy that arms a pause in the window step 1 cannot see.

    Step 1 only READS, so the pass's very first ``write`` is the lease acquire. Arming
    the pause immediately before that write reproduces the exact TOCTOU interleaving:
    the step-1 check saw no pause, and the acquire is next.
    """

    def __init__(self, db, *, now, minutes):
        self._db = db
        self._now = now
        self._minutes = minutes
        self.armed_at_until = None

    async def read(self, fn):
        return await self._db.read(fn)

    async def write(self, fn):
        if self.armed_at_until is None:
            self.armed_at_until = await self._db.write(
                lambda c: pause_ops.pause(c, now=self._now, minutes=self._minutes)
            )
        return await self._db.write(fn)


async def test_pause_armed_between_step1_and_acquire_blocks_the_pass(tmp_path):
    """BLOCKER: the pause check must live INSIDE the acquiring transaction.

    A pause armed after the step-1 read bumps the epoch to E+1, but ``acquire`` bumps it
    straight to E+2 — the pass then owns the freshest epoch, every guarded write passes
    and ``pause_until`` is never read again, so the owner who hit the emergency stop
    watches tabs close for minutes (§7 "этого достаточно, чтобы остановить уже идущий
    проход"). Reddens if the in-transaction check is removed: the pass starts and
    reports ok/no_ready_instances instead of `paused`.
    """
    db = await _make_db(tmp_path)
    now0 = 30_000_000
    racer = _PauseRacer(db, now=now0, minutes=30)

    res = await runner.run_pass(racer, Registry(), _settings(), now=now0 + 1)
    assert res["status"] == "paused"
    assert res["until"] == racer.armed_at_until
    # Nothing started: no passes row, and the blocked pass never took the lease
    # (acquire was not reached, so the slot was never written at all).
    assert await db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM passes").fetchone()) == (0,)
    assert (await db.read(lease.read_lease))["until"] is None


async def test_dry_run_is_not_muted_by_the_in_transaction_pause_check(tmp_path):
    """§7: looking at the plan is exactly why a pause is taken, so ``dry_run`` must NOT
    be blocked by the same-transaction check (only the real pass is)."""
    db = await _make_db(tmp_path)
    now0 = 40_000_000
    racer = _PauseRacer(db, now=now0, minutes=30)
    res = await runner.run_pass(racer, Registry(), _settings(), dry_run=True, now=now0 + 1)
    assert res["status"] == "dry_run"


# --- a fenced pass releases ITS OWN slot; the pause does not do it for it ----
async def test_fenced_pass_releases_its_own_slot_pause_does_not(tmp_path):
    """§7 "снятие руками (DELETE /api/pause) запускает проход немедленно" — WITHOUT
    ever letting two passes overlap.

    ``release`` is keyed on the OWNER (a fresh uuid per acquire), not on the epoch, so
    a pass fenced by a pause can still hand its slot back when it finishes. Keying it on
    the epoch too meant the fenced pass could never release: renewal stops,
    ``pass_lease_until`` sits in the future for the whole ``LEASE_TTL_MS`` (10 min), and
    a pause lifted after two minutes is answered with ``lease_unavailable`` until the TTL
    burns out.

    The pause itself must NOT free the slot: ``run_phase_a`` / ``run_window_merge`` send
    their browser command BEFORE their first guarded write, so a slot freed at pause time
    lets the resume's pass start while the fenced one is still inside ``send_command``.
    Reddens both ways — restore the epoch condition in ``release`` and the last acquire
    fails; free the slot inside ``pause()`` and the mid-flight assertion fails.
    """
    db = await _make_db(tmp_path)
    acquired, epoch = await db.write(lambda c: lease.acquire(c, "pass-1", 0, 600_000))
    assert acquired
    assert (await db.read(lease.read_lease))["until"] == 600_000  # held

    await db.write(lambda c: pause_ops.pause(c, now=1_000, minutes=30))

    # MID-FLIGHT: the pass is fenced (its guarded writes fail) but STILL owns the slot,
    # so nobody else can start while it may still be talking to a browser.
    with pytest.raises(lease.LeaseLost):
        await db.write(lease.guarded(epoch, lambda c: c.execute(
            "INSERT INTO settings (key, value) VALUES ('x','y')")))
    assert (await db.read(lease.read_lease))["until"] == 600_000
    blocked, _ = await db.write(lambda c: lease.acquire(c, "pass-2", 2_000, 600_000))
    assert blocked is False

    # The fenced pass reaches its `finally` and releases despite the moved epoch.
    await db.write(lambda c: lease.release(c, "pass-1"))
    assert (await db.read(lease.read_lease))["until"] == 0

    # Now the pass triggered by the manual resume acquires at once — no TTL wait.
    acquired2, epoch2 = await db.write(lambda c: lease.acquire(c, "pass-2", 3_000, 600_000))
    assert acquired2 and epoch2 > epoch


async def test_release_still_never_clears_a_foreign_lease(tmp_path):
    """The invariant owner-keying must preserve: a pass whose lease EXPIRED and was
    taken over by another must not clear the new holder's slot on its way out."""
    db = await _make_db(tmp_path)
    _, epoch1 = await db.write(lambda c: lease.acquire(c, "pass-1", 0, 1_000))
    # pass-1's TTL expires; pass-2 takes over.
    ok, epoch2 = await db.write(lambda c: lease.acquire(c, "pass-2", 5_000, 600_000))
    assert ok and epoch2 > epoch1
    # pass-1 finally wakes up and releases: a NO-OP, pass-2 keeps the lease.
    await db.write(lambda c: lease.release(c, "pass-1"))
    assert (await db.read(lease.read_lease))["owner"] == "pass-2"
    blocked, _ = await db.write(lambda c: lease.acquire(c, "pass-3", 6_000, 600_000))
    assert blocked is False
