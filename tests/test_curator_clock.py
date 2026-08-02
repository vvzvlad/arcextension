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
    assert clockmod.is_continuity_break(None, base) is False              # empty park => not a break
    assert clockmod.is_continuity_break(base, dict(base)) is False        # identical
    assert clockmod.is_continuity_break(base, {**base, "user_version": 2}) is True
    assert clockmod.is_continuity_break(base, {**base, "idle_minutes": 30}) is True
    assert clockmod.is_continuity_break(base, {**base, "main_instance_id": "m2"}) is True
    assert clockmod.is_continuity_break(base, {**base, "db_uuid": "u2"}) is True


# --- first-run policy: NO fingerprint + a populated fleet IS a break ---------
def test_no_fingerprint_is_a_break_only_when_the_fleet_is_populated():
    # §7 lists "чистая БД" among the breaks: every tab arrives with age_unknown=1 and
    # last_active_at=now, so an hour later the WHOLE fleet turns eligible at once. With
    # a fleet present that must wait for a click; with a completely empty DB (nothing
    # ever connected) there is nothing to drain and no reason to make a new owner wait.
    current = {"db_uuid": None, "user_version": 1, "idle_minutes": 60,
               "main_instance_id": "main"}
    assert clockmod.is_continuity_break(None, current, fleet_populated=True) is True
    assert clockmod.is_continuity_break(None, current, fleet_populated=False) is False


async def test_fleet_populated_reader(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    try:
        assert await db.read(clockmod.fleet_populated) is False
        await db.write(lambda c: c.execute(
            "INSERT INTO instances (id, conn_epoch, connected) VALUES ('main', 1, 1)"))
        assert await db.read(clockmod.fleet_populated) is True
    finally:
        await db.close()


# --- WARNING-2: the EXTERNAL restore marker ---------------------------------
def test_restore_marker_unset_keeps_the_component_absent(tmp_path):
    # No marker configured => None => the fingerprint component is absent and an
    # installation that never set the knob behaves exactly as before (no regression).
    assert clockmod.read_restore_marker("") is None
    assert clockmod.read_restore_marker(None) is None
    assert clockmod.read_restore_marker("   ") is None


def test_restore_marker_change_is_a_break(tmp_path):
    marker = tmp_path / "continuity-marker"
    marker.write_text("11111111-1111-1111-1111-111111111111\n")
    before = clockmod.read_restore_marker(str(marker))
    assert before not in (None, clockmod.MARKER_MISSING, clockmod.MARKER_UNREADABLE)

    stored = {"db_uuid": "u1", "user_version": 1, "idle_minutes": 60,
              "main_instance_id": "main", "restore_marker": before}
    # Same marker => no break (the digest is stable across reads).
    same = {**stored, "restore_marker": clockmod.read_restore_marker(str(marker))}
    assert clockmod.is_continuity_break(stored, same) is False

    # The operator restored from backup and wrote a FRESH uuid => break.
    marker.write_text("22222222-2222-2222-2222-222222222222\n")
    after = {**stored, "restore_marker": clockmod.read_restore_marker(str(marker))}
    assert after["restore_marker"] != before
    assert clockmod.is_continuity_break(stored, after) is True

    # A marker that DISAPPEARED is a real observation: a break, exactly once, no raise.
    marker.unlink()
    gone = clockmod.read_restore_marker(str(marker))
    assert gone == clockmod.MARKER_MISSING
    assert clockmod.is_continuity_break(stored, {**stored, "restore_marker": gone}) is True
    # ... and a persistently absent marker does not re-break every pass.
    assert clockmod.is_continuity_break(
        {**stored, "restore_marker": gone}, {**stored, "restore_marker": gone}
    ) is False


def test_toggling_detection_on_or_off_is_a_documented_one_off_break():
    """"Not configured" is a RECORDED state, so switching the detector on or off is a
    break — exactly like changing IDLE_MINUTES or MAIN_INSTANCE_ID, the fingerprint's
    other config inputs (§7). Deliberate and documented in .env.example: a silent
    switch-OFF would drop restore detection with no signal at all, which is worse than
    one dry_run the operator asked for by editing the config. Staying unconfigured
    forever must remain silent.
    """
    base = {"db_uuid": "u1", "user_version": 1, "idle_minutes": 60,
            "main_instance_id": "main"}
    off = {**base, "restore_marker": None}
    on = {**base, "restore_marker": "digest-A"}

    assert clockmod.is_continuity_break(off, off) is False   # never configured: silent
    assert clockmod.is_continuity_break(on, on) is False     # configured, unchanged
    assert clockmod.is_continuity_break(off, on) is True     # turned ON
    assert clockmod.is_continuity_break(on, off) is True     # turned OFF


def test_marker_read_is_size_capped(tmp_path):
    # A typo pointing RESTORE_MARKER_PATH at a backup/log must not pull an unbounded
    # file into memory each pass: only the first _MARKER_READ_LIMIT bytes are hashed.
    big = tmp_path / "huge"
    big.write_bytes(b"x" * (clockmod._MARKER_READ_LIMIT + 4096))
    capped = tmp_path / "capped"
    capped.write_bytes(b"x" * clockmod._MARKER_READ_LIMIT)
    assert clockmod.read_restore_marker(str(big)) == clockmod.read_restore_marker(str(capped))


def test_transient_read_failure_is_not_a_break(tmp_path, monkeypatch):
    """A wedged/unmounted volume must NOT manufacture a break: the resulting
    resume_pending latch is cleared only by a real pass, which the latch itself blocks —
    the service would need a human click to leave a state a flaky mount put it in.
    Reddens if OSError is mapped to the same value as a genuinely missing file."""
    marker = tmp_path / "continuity-marker"
    marker.write_text("uuid-1")
    digest = clockmod.read_restore_marker(str(marker))

    def _boom(*_a, **_k):
        raise PermissionError("volume not mounted")
    monkeypatch.setattr("builtins.open", _boom)

    unreadable = clockmod.read_restore_marker(str(marker))
    assert unreadable == clockmod.MARKER_UNREADABLE

    stored = {"db_uuid": "u1", "user_version": 1, "idle_minutes": 60,
              "main_instance_id": "main", "restore_marker": digest}
    assert clockmod.is_continuity_break(
        stored, {**stored, "restore_marker": unreadable}
    ) is False


async def test_unreadable_marker_does_not_erase_the_known_digest(tmp_path, monkeypatch):
    """Storing must not overwrite the last KNOWN digest with the "read failed" sentinel:
    the stored value is the only thing a later successful read can be compared against.
    Reddens if store_fingerprint persists MARKER_UNREADABLE."""
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    try:
        good = {"db_uuid": "u1", "user_version": 1, "idle_minutes": 60,
                "main_instance_id": "main", "restore_marker": "digest-A"}
        await db.write(lambda c: clockmod.store_fingerprint(c, good))
        # A pass during the outage stores a fingerprint carrying the sentinel...
        await db.write(lambda c: clockmod.store_fingerprint(
            c, {**good, "restore_marker": clockmod.MARKER_UNREADABLE}))
        stored = await db.read(clockmod.read_stored_fingerprint)
        assert stored["restore_marker"] == "digest-A"  # carried over, not erased
        # ... so once the mount is back, a REAL change is still detected.
        assert clockmod.is_continuity_break(
            stored, {**good, "restore_marker": "digest-B"}) is True
    finally:
        await db.close()


async def test_marker_read_is_time_bounded_and_off_the_shared_pool(tmp_path, monkeypatch):
    """A wedged mount must degrade the DETECTOR, not the service.

    ``open()`` on a hung mount is uninterruptible, so the read runs on its own
    single-thread executor (never the default one ``Database.read`` uses) and gives up
    after a bound. Reddens if the timeout is dropped: this test hangs instead of
    returning. The parked thread is expected to survive — what matters is that the
    caller returns, that it returns the "no information" value, and that a SECOND call
    (queued behind the wedged worker) also returns instead of hanging forever.
    """
    import threading

    released = threading.Event()

    def _hang(*_a, **_k):
        released.wait(30)          # simulates an uninterruptible syscall
        raise FileNotFoundError()
    monkeypatch.setattr("builtins.open", _hang)

    try:
        first = await clockmod.read_restore_marker_async(
            str(tmp_path / "marker"), timeout_s=0.05)
        assert first == clockmod.MARKER_UNREADABLE
        # The worker is still parked; a later pass must not hang behind it either.
        second = await clockmod.read_restore_marker_async(
            str(tmp_path / "marker"), timeout_s=0.05)
        assert second == clockmod.MARKER_UNREADABLE
    finally:
        released.set()

    # The executor is a dedicated one, so the loop's default pool (Database.read) is
    # untouched: a plain DB read still works while the marker worker is wedged.
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    try:
        assert await db.read(clockmod.fleet_populated) is False
    finally:
        await db.close()


async def test_marker_async_skips_the_read_entirely_when_unconfigured(tmp_path, monkeypatch):
    """An unconfigured marker must not touch the filesystem at all."""
    def _boom(*_a, **_k):
        raise AssertionError("must not open anything when the path is empty")
    monkeypatch.setattr("builtins.open", _boom)
    assert await clockmod.read_restore_marker_async("") is None
    assert await clockmod.read_restore_marker_async(None) is None


async def test_legacy_fingerprint_without_the_marker_key_is_not_a_break(tmp_path):
    """Upgrade path: a fingerprint written before this release carries no restore_marker
    component. Comparing a fresh digest against "absent" would flag EVERY existing
    install as restored on its first pass — and the resulting latch needs a human click.
    The component is skipped until one pass has recorded it. Reddens if the key is
    compared unconditionally."""
    marker = tmp_path / "continuity-marker"
    marker.write_text("seed")
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    try:
        digest = clockmod.read_restore_marker(str(marker))
        fp = await db.read(lambda c: clockmod.current_fingerprint(
            c, idle_minutes=60, main_instance_id="main", restore_marker=digest))
        assert fp["restore_marker"] == digest

        legacy = {k: v for k, v in fp.items() if k != "restore_marker"}
        assert clockmod.is_continuity_break(legacy, fp) is False
        # Once recorded, the component IS compared again.
        assert clockmod.is_continuity_break(fp, {**fp, "restore_marker": "other"}) is True
    finally:
        await db.close()


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
