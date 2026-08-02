"""Server-clock guard + continuity-break fingerprint tests (§7)."""

import pytest

from src.curator import clock as clockmod
from src.curator.clock import ClockGuard
from src.db.access import Database


def _sources(wall_values, mono_values):
    wall_iter = iter(wall_values)
    mono_iter = iter(mono_values)
    return (lambda: next(wall_iter)), (lambda: next(mono_iter))


def test_clock_step_detected_then_rebaselined():
    # threshold 300s (PASS_INTERVAL_MIN=5). Baseline at t=0. Then a check where the
    # WALL jumped +3600s while MONOTONIC advanced only +5s (VM resume from suspend):
    # skew ~= 3595s > 300 => detected. The NEXT check advances both normally => small.
    wall, mono = _sources(
        [1_000.0, 4_600.0, 4_900.0],   # baseline, +3600 (step), +300 normal
        [500.0, 505.0, 805.0],         # baseline, +5, +300
    )
    g = ClockGuard(300.0, wall=wall, mono=mono)
    skew1 = g.check()
    assert g.exceeds(skew1)
    assert skew1 == pytest.approx(3595.0, abs=1.0)
    # Re-baselined: a normal interval is NOT flagged.
    skew2 = g.check()
    assert not g.exceeds(skew2)
    assert skew2 == pytest.approx(0.0, abs=1.0)


def test_clock_normal_operation_no_flag():
    wall, mono = _sources([1000.0, 1300.0], [500.0, 800.0])
    g = ClockGuard(300.0, wall=wall, mono=mono)
    assert not g.exceeds(g.check())


# --- continuity fingerprint --------------------------------------------------
def test_is_continuity_break_matrix():
    base = {"db_uuid": "u1", "user_version": 1, "idle_minutes": 60, "main_instance_id": "main"}
    assert clockmod.is_continuity_break(None, base) is False              # no prior => not a break
    assert clockmod.is_continuity_break(base, dict(base)) is False        # identical
    assert clockmod.is_continuity_break(base, {**base, "user_version": 2}) is True
    assert clockmod.is_continuity_break(base, {**base, "idle_minutes": 30}) is True
    assert clockmod.is_continuity_break(base, {**base, "main_instance_id": "m2"}) is True
    assert clockmod.is_continuity_break(base, {**base, "db_uuid": "u2"}) is True


async def test_fingerprint_store_and_read_roundtrip(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    try:
        cur = await db.read(lambda c: clockmod.current_fingerprint(
            c, idle_minutes=60, main_instance_id="main"))
        assert cur["db_uuid"] is None  # fresh DB
        # Storing materializes a db_uuid so the NEXT pass sees a stable identity.
        await db.write(lambda c: clockmod.store_fingerprint(c, cur))
        stored = await db.read(clockmod.read_stored_fingerprint)
        assert stored["db_uuid"] is not None
        assert stored["user_version"] == cur["user_version"]
        # A second current-fingerprint now carries the same db_uuid => no break.
        cur2 = await db.read(lambda c: clockmod.current_fingerprint(
            c, idle_minutes=60, main_instance_id="main"))
        assert clockmod.is_continuity_break(stored, cur2) is False
    finally:
        await db.close()
