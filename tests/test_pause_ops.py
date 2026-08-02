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
