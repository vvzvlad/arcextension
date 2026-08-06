"""Server-clock guard tests (§7)."""

import pytest

from src.curator.clock import ClockGuard


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
