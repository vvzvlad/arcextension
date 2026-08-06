"""Stop/start WRITE side unit tests (§7 «Пауза» → the indefinite emergency stop).

Covers the shared :mod:`src.curator.pause` primitives and the runner's stop lifecycle
directly (no HTTP), so each mechanic reddens if its guard drops:

* the TTL shift is the ACTUAL stop duration (however long the stop lasted),
* the shift is applied EXACTLY ONCE per stop (double-shift guard),
* a re-press while stopped keeps the ORIGINAL stop time (idempotency),
* the epoch bump stops an in-flight pass at its next guarded write,
* a stopped curator blocks the pass with ``{"status": "stopped"}`` — and a
  ``confirm_pending`` click still loses to a live stop,
* the stop check inside the acquiring transaction closes the TOCTOU window.
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
        quarantine_ttl_min=1440, main_instance_id="main", max_actions_per_pass=20,
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


# --- stop is idempotent: a re-press keeps the ORIGINAL timestamp --------------
async def test_stop_repress_keeps_original_timestamp(tmp_path):
    db = await _make_db(tmp_path)
    first = await db.write(lambda c: pause_ops.stop(c, now=1_000))
    assert first == 1_000
    assert await db.read(pause_ops.read_stopped_at) == 1_000

    # A nervous re-press 4s later must NOT move the start — the TTL shift counts the
    # FULL stop duration from the first press (§7).
    second = await db.write(lambda c: pause_ops.stop(c, now=5_000))
    assert second == 1_000
    assert await db.read(pause_ops.read_stopped_at) == 1_000


# --- the shift is the ACTUAL stop duration ------------------------------------
async def test_start_shift_is_actual_stop_duration(tmp_path):
    db = await _make_db(tmp_path)
    now0 = 1_000_000
    await db.write(lambda c: pause_ops.stop(c, now=now0))

    # A 2h protection issued AT stop time (until > stopped_at → shiftable) and a stale
    # one that expired BEFORE the stop started (until <= stopped_at → left alone, §7).
    prot = now0 + 7_200_000
    stale = now0 - 1
    await _add_exemption(db, "main", "https://prot", prot)
    await _add_exemption(db, "main", "https://stale", stale)
    await _add_quarantine(db, "main", "https://q", prot)

    # Start after 5 minutes of stop.
    now1 = now0 + 5 * 60_000
    shift = await db.write(lambda c: pause_ops.apply_resume_shift(c, now=now1))
    assert shift == 5 * 60_000  # exactly the stopped time

    ex = {r[0]: r[1] for r in await db.read(
        lambda c: c.execute("SELECT url, until FROM exemptions").fetchall())}
    q = {r[0]: r[1] for r in await db.read(
        lambda c: c.execute("SELECT url, until FROM quarantine").fetchall())}
    assert ex["https://prot"] == prot + 5 * 60_000
    assert q["https://q"] == prot + 5 * 60_000
    # The already-expired protection is untouched.
    assert ex["https://stale"] == stale

    # The stop is fully cleared.
    assert await db.read(pause_ops.read_stopped_at) is None


async def test_start_shift_covers_a_long_stop_through_a_repress(tmp_path):
    # The idempotency requirement end to end: stop at T, re-press at T+1h, start at
    # T+2h → the shift is the FULL 2h, not the 1h since the re-press.
    db = await _make_db(tmp_path)
    t0 = 10_000_000
    await db.write(lambda c: pause_ops.stop(c, now=t0))
    await db.write(lambda c: pause_ops.stop(c, now=t0 + 3_600_000))  # re-press
    prot = t0 + 10 * 3_600_000
    await _add_exemption(db, "main", "https://p", prot)

    shift = await db.write(lambda c: pause_ops.apply_resume_shift(c, now=t0 + 7_200_000))
    assert shift == 7_200_000
    assert await db.read(
        lambda c: c.execute("SELECT until FROM exemptions").fetchone()) == (prot + 7_200_000,)


# --- the shift is applied EXACTLY ONCE per stop (double-shift guard) ----------
async def test_shift_applied_exactly_once(tmp_path):
    db = await _make_db(tmp_path)
    now0 = 2_000_000
    await db.write(lambda c: pause_ops.stop(c, now=now0))
    prot = now0 + 7_200_000
    await _add_exemption(db, "main", "https://p", prot)

    now1 = now0 + 10 * 60_000
    first = await db.write(lambda c: pause_ops.apply_resume_shift(c, now=now1))
    assert first == 10 * 60_000
    after1 = await db.read(
        lambda c: c.execute("SELECT until FROM exemptions").fetchone()[0])
    assert after1 == prot + 10 * 60_000

    # A SECOND call (a repeated start/confirm) must shift NOTHING — the stop is cleared.
    second = await db.write(lambda c: pause_ops.apply_resume_shift(c, now=now1 + 999))
    assert second == 0
    after2 = await db.read(
        lambda c: c.execute("SELECT until FROM exemptions").fetchone()[0])
    assert after2 == after1


async def test_start_does_not_clear_the_resume_pending_latch(tmp_path):
    # apply_resume_shift must NOT touch the threshold latch: the confirming pass
    # consumes it (the runner degrades confirm_pending when nothing is armed, so a
    # latch cleared here would turn the confirm into an ordinary pass that re-latches).
    from src.db.settings_store import set_setting

    db = await _make_db(tmp_path)
    await db.write(lambda c: pause_ops.stop(c, now=1_000))
    await db.write(lambda c: set_setting(
        c, pause_ops.RESUME_PENDING_KEY, '{"since": 1, "plan": {}}'))
    await db.write(lambda c: pause_ops.apply_resume_shift(c, now=2_000))
    assert await db.read(pause_ops.read_stopped_at) is None
    assert await db.read(lambda c: get_setting(c, pause_ops.RESUME_PENDING_KEY))


# --- the epoch bump stops an IN-FLIGHT pass at its next guarded write --------
async def test_stop_fences_in_flight_pass(tmp_path):
    db = await _make_db(tmp_path)
    # A pass is running: it holds the lease at epoch `e`.
    acquired, e = await db.write(lambda c: lease.acquire(c, "pass-1", 0, 600_000))
    assert acquired
    # Pressing stop mid-pass bumps the epoch.
    await db.write(lambda c: pause_ops.stop(c, now=1_000))
    # The in-flight pass's NEXT guarded write now changes zero rows → LeaseLost (§7).
    with pytest.raises(lease.LeaseLost):
        await db.write(lease.guarded(e, lambda c: c.execute(
            "INSERT INTO settings (key, value) VALUES ('x','y')")))


# --- a stopped curator blocks the pass; confirm_pending loses too ------------
async def test_stopped_blocks_the_pass_with_status_stopped(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    now0 = 10_000_000
    await db.write(lambda c: pause_ops.stop(c, now=now0))

    res = await runner.run_pass(db, reg, _settings(), now=now0 + 60_000)
    assert res == {"status": "stopped", "since": now0}
    # The stop is INDEFINITE: hours later the answer is the same (nothing expires).
    res2 = await runner.run_pass(db, reg, _settings(), now=now0 + 48 * 3_600_000)
    assert res2 == {"status": "stopped", "since": now0}
    # No passes row was ever written.
    assert await db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM passes").fetchone()) == (0,)


async def test_confirm_pending_refuses_under_active_stop(tmp_path):
    db = await _make_db(tmp_path)
    reg = Registry()
    now0 = 20_000_000
    await db.write(lambda c: pause_ops.stop(c, now=now0))
    # A stop pressed after the deferred plan was shown wins even over a confirm: the
    # emergency stop is not bypassable.
    res = await runner.run_pass(
        db, reg, _settings(), confirm_pending=True, now=now0 + 1_000
    )
    assert res["status"] == "stopped" and res["since"] == now0


# --- a stop armed BETWEEN the step-1 read and the step-2 acquire still wins --
class _StopRacer:
    """A ``Database`` proxy that arms the stop in the window step 1 cannot see.

    Step 1 only READS, so the pass's very first ``write`` is the lease acquire. Arming
    the stop immediately before that write reproduces the exact TOCTOU interleaving:
    the step-1 check saw no stop, and the acquire is next.
    """

    def __init__(self, db, *, now):
        self._db = db
        self._now = now
        self.armed_at = None

    async def read(self, fn):
        return await self._db.read(fn)

    async def write(self, fn):
        if self.armed_at is None:
            self.armed_at = await self._db.write(
                lambda c: pause_ops.stop(c, now=self._now)
            )
        return await self._db.write(fn)


async def test_stop_armed_between_step1_and_acquire_blocks_the_pass(tmp_path):
    """BLOCKER: the stop check must live INSIDE the acquiring transaction.

    A stop pressed after the step-1 read bumps the epoch to E+1, but ``acquire`` bumps
    it straight to E+2 — the pass then owns the freshest epoch, every guarded write
    passes and ``curator_stopped_at`` is never read again, so the owner who hit the
    emergency stop watches tabs close for minutes (§7 "этого достаточно, чтобы
    остановить уже идущий проход"). Reddens if the in-transaction check is removed: the
    pass starts and reports ok/no_ready_instances instead of `stopped`.
    """
    db = await _make_db(tmp_path)
    now0 = 30_000_000
    racer = _StopRacer(db, now=now0)

    res = await runner.run_pass(racer, Registry(), _settings(), now=now0 + 1)
    assert res["status"] == "stopped"
    assert res["since"] == racer.armed_at
    # Nothing started: no passes row, and the blocked pass never took the lease
    # (acquire was not reached, so the slot was never written at all).
    assert await db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM passes").fetchone()) == (0,)
    assert (await db.read(lease.read_lease))["until"] is None


async def test_dry_run_is_not_muted_by_the_in_transaction_stop_check(tmp_path):
    """§7: looking at the plan is exactly why the stop is pressed, so ``dry_run`` must
    NOT be blocked by the same-transaction check (only the real pass is)."""
    db = await _make_db(tmp_path)
    now0 = 40_000_000
    racer = _StopRacer(db, now=now0)
    res = await runner.run_pass(racer, Registry(), _settings(), dry_run=True, now=now0 + 1)
    assert res["status"] == "dry_run"


# --- a fenced pass releases ITS OWN slot; the stop does not do it for it ----
async def test_fenced_pass_releases_its_own_slot_stop_does_not(tmp_path):
    """§7 "снятие руками (DELETE /api/pause) запускает проход немедленно" — WITHOUT
    ever letting two passes overlap.

    ``release`` is keyed on the OWNER (a fresh uuid per acquire), not on the epoch, so
    a pass fenced by a stop can still hand its slot back when it finishes. Keying it on
    the epoch too meant the fenced pass could never release: renewal stops,
    ``pass_lease_until`` sits in the future for the whole ``LEASE_TTL_MS`` (10 min), and
    a stop lifted after two minutes is answered with ``lease_unavailable`` until the TTL
    burns out.

    The stop itself must NOT free the slot: ``run_phase_a`` / ``run_window_merge`` send
    their browser command BEFORE their first guarded write, so a slot freed at stop time
    lets the start's pass begin while the fenced one is still inside ``send_command``.
    Reddens both ways — restore the epoch condition in ``release`` and the last acquire
    fails; free the slot inside ``stop()`` and the mid-flight assertion fails.
    """
    db = await _make_db(tmp_path)
    acquired, epoch = await db.write(lambda c: lease.acquire(c, "pass-1", 0, 600_000))
    assert acquired
    assert (await db.read(lease.read_lease))["until"] == 600_000  # held

    await db.write(lambda c: pause_ops.stop(c, now=1_000))

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

    # Now the pass triggered by the manual start acquires at once — no TTL wait.
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
