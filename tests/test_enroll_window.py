"""Enrollment window helpers (§13) — arm / read / close on the ``settings`` table.

Exercises the read-time-compare contract directly (no HTTP, no timer): the window is
open purely as a function of the stored deadline and the caller's ``now``, so a
"restart" (a fresh read of the same row) sees exactly the same state.
"""

from src.curator import enroll
from src.db.access import Database
from src.db.settings_store import get_setting


async def _make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def test_arm_clamps_an_absurd_window_length(tmp_path):
    # A huge ENROLL_WINDOW_MIN would make the window effectively always-open and void
    # the short-window guarantee; arm clamps to ENROLL_WINDOW_MAX_MIN (mirrors pause).
    db = await _make_db(tmp_path)
    try:
        now = 1_000
        state = await db.write(
            lambda c: enroll.arm_enroll_window(c, now=now, minutes=525_600)  # a year
        )
        assert state.until == now + enroll.ENROLL_WINDOW_MAX_MIN * 60_000
        assert state.seconds_remaining == enroll.ENROLL_WINDOW_MAX_MIN * 60
        # And a non-positive length is floored to one minute (still a real, closeable window).
        floored = await db.write(
            lambda c: enroll.arm_enroll_window(c, now=now, minutes=0)
        )
        assert floored.until == now + 60_000
    finally:
        await db.close()


async def test_expired_window_reports_no_code_or_until(tmp_path):
    # An expired-but-not-cleared window reads closed AND surfaces code/until = None, so a
    # caller rendering state.code without checking state.open can never show a dead code.
    db = await _make_db(tmp_path)
    try:
        armed = await db.write(lambda c: enroll.arm_enroll_window(c, now=1_000, minutes=10))
        expired = await db.read(
            lambda c: enroll.read_enroll_window(c, now=armed.until + 1)
        )
        assert expired.open is False
        assert expired.seconds_remaining == 0
        assert expired.code is None
        assert expired.until is None
        # The underlying rows are NOT cleared (only the returned state hides them): a
        # later arm still overwrites them. Prove the row is still physically present.
        raw_until = await db.read(
            lambda c: get_setting(c, enroll.ENROLL_WINDOW_UNTIL_KEY)
        )
        assert raw_until == str(armed.until)
    finally:
        await db.close()


async def test_arm_opens_window_with_future_deadline_and_code(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = 1_000
        state = await db.write(
            lambda c: enroll.arm_enroll_window(c, now=now, minutes=10)
        )
        # Armed: open, full duration remaining, a code minted.
        assert state.open is True
        assert state.until == now + 10 * 60_000
        assert state.seconds_remaining == 600
        assert state.code and len(state.code) == enroll._CODE_LENGTH

        # The deadline + code are actually persisted in the settings table.
        until_row = await db.read(
            lambda c: get_setting(c, enroll.ENROLL_WINDOW_UNTIL_KEY)
        )
        code_row = await db.read(
            lambda c: get_setting(c, enroll.ENROLL_WINDOW_CODE_KEY)
        )
        assert int(until_row) == state.until
        assert code_row == state.code
    finally:
        await db.close()


async def test_read_open_before_expiry_closed_after(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = 1_000
        armed = await db.write(
            lambda c: enroll.arm_enroll_window(c, now=now, minutes=10)
        )
        until = armed.until

        # Before the deadline: open, positive seconds remaining.
        before = await db.read(lambda c: enroll.read_enroll_window(c, now=until - 5_000))
        assert before.open is True
        assert before.seconds_remaining == 5
        assert before.code == armed.code

        # At/after the deadline: closed, zero seconds.
        at = await db.read(lambda c: enroll.read_enroll_window(c, now=until))
        assert at.open is False and at.seconds_remaining == 0
        after = await db.read(lambda c: enroll.read_enroll_window(c, now=until + 1))
        assert after.open is False and after.seconds_remaining == 0
    finally:
        await db.close()


async def test_read_time_compare_survives_a_restart(tmp_path):
    # Prove there is no timer: the SAME stored deadline reads open or closed purely from
    # the `now` handed in. A "restart" is just a fresh read of the persisted row — it
    # neither closes the window early nor keeps it open past its deadline.
    db = await _make_db(tmp_path)
    try:
        now = 1_000
        armed = await db.write(
            lambda c: enroll.arm_enroll_window(c, now=now, minutes=10)
        )
        until = armed.until
    finally:
        await db.close()

    # Reopen the SAME database file (a genuine process restart — no in-memory state).
    db2 = await _make_db(tmp_path)
    try:
        still_open = await db2.read(
            lambda c: enroll.read_enroll_window(c, now=until - 60_000)
        )
        assert still_open.open is True
        assert still_open.seconds_remaining == 60
        assert still_open.code == armed.code  # the code persisted across the restart

        # And once the wall clock is past the deadline, the same row reads closed —
        # nothing had to fire to close it.
        expired = await db2.read(
            lambda c: enroll.read_enroll_window(c, now=until + 60_000)
        )
        assert expired.open is False and expired.seconds_remaining == 0
    finally:
        await db2.close()


async def test_close_clears_the_window(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = 1_000
        armed = await db.write(
            lambda c: enroll.arm_enroll_window(c, now=now, minutes=10)
        )
        # Well within the window, but explicitly closed → reads closed with no code.
        await db.write(enroll.close_enroll_window)
        state = await db.read(
            lambda c: enroll.read_enroll_window(c, now=armed.until - 1_000)
        )
        assert state.open is False
        assert state.seconds_remaining == 0
        assert state.code is None
        assert state.until is None
    finally:
        await db.close()


async def test_rearm_mints_a_fresh_code(tmp_path):
    # Issue #35: "код новый на каждое открытие окна" — a re-open replaces the code.
    db = await _make_db(tmp_path)
    try:
        first = await db.write(lambda c: enroll.arm_enroll_window(c, now=1_000, minutes=10))
        second = await db.write(lambda c: enroll.arm_enroll_window(c, now=2_000, minutes=10))
        assert second.code != first.code
        # The stored code is the latest one.
        stored = await db.read(lambda c: get_setting(c, enroll.ENROLL_WINDOW_CODE_KEY))
        assert stored == second.code
    finally:
        await db.close()


async def test_absent_window_reads_closed(tmp_path):
    # A fresh DB has never armed a window → closed, no code, no deadline.
    db = await _make_db(tmp_path)
    try:
        state = await db.read(lambda c: enroll.read_enroll_window(c, now=1_000))
        assert state.open is False
        assert state.seconds_remaining == 0
        assert state.code is None
        assert state.until is None
    finally:
        await db.close()
