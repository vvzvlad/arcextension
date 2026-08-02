"""Lease + fencing-epoch tests (§7 "Аренда с fencing").

Proves: acquire is mutually exclusive; a lost/moved epoch fences a guarded write to
ZERO rows (raising LeaseLost so the pass stops); renewal only extends a still-held
lease; release never clears someone else's.
"""

import pytest

from src.curator import lease
from src.db.access import Database


async def _db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def test_acquire_is_exclusive_and_bumps_epoch(tmp_path):
    db = await _db(tmp_path)
    try:
        ok1, e1 = await db.write(lambda c: lease.acquire(c, "A", now=1000, ttl_ms=10_000))
        assert ok1 and e1 == 1
        # A second acquire while the lease is live fails; epoch is NOT bumped.
        ok2, e2 = await db.write(lambda c: lease.acquire(c, "B", now=1500, ttl_ms=10_000))
        assert ok2 is False and e2 == 1
    finally:
        await db.close()


async def test_acquire_after_expiry_bumps_epoch(tmp_path):
    db = await _db(tmp_path)
    try:
        ok1, e1 = await db.write(lambda c: lease.acquire(c, "A", now=1000, ttl_ms=10_000))
        # After the lease expires (now past until) a new owner acquires with a NEW epoch.
        ok2, e2 = await db.write(lambda c: lease.acquire(c, "B", now=20_000, ttl_ms=10_000))
        assert ok1 and ok2
        assert e2 == e1 + 1
    finally:
        await db.close()


async def test_guard_raises_when_epoch_moved(tmp_path):
    db = await _db(tmp_path)
    try:
        _, epoch = await db.write(lambda c: lease.acquire(c, "A", now=1000, ttl_ms=10_000))
        # Same epoch: guard passes (no raise).
        await db.write(lease.guarded(epoch, lambda c: None))
        # Someone bumps the epoch (a pause, or another pass acquiring after expiry).
        await db.write(lambda c: lease.bump_epoch(c))
        with pytest.raises(lease.LeaseLost):
            await db.write(lease.guarded(epoch, lambda c: c.execute(
                "INSERT INTO settings (key, value) VALUES ('x', '1')"
            )))
        # The fenced write rolled back: its side effect did NOT land.
        val = await db.read(lambda c: c.execute(
            "SELECT value FROM settings WHERE key='x'").fetchone())
        assert val is None
    finally:
        await db.close()


async def test_renew_only_while_held(tmp_path):
    db = await _db(tmp_path)
    try:
        _, epoch = await db.write(lambda c: lease.acquire(c, "A", now=1000, ttl_ms=10_000))
        assert await db.write(lambda c: lease.renew(c, "A", epoch, now=2000, ttl_ms=10_000)) is True
        # Wrong owner or wrong epoch cannot renew.
        assert await db.write(lambda c: lease.renew(c, "B", epoch, now=3000, ttl_ms=10_000)) is False
        assert await db.write(lambda c: lease.renew(c, "A", epoch + 1, now=3000, ttl_ms=10_000)) is False
    finally:
        await db.close()


async def test_release_only_own_lease(tmp_path):
    db = await _db(tmp_path)
    try:
        _, epoch = await db.write(lambda c: lease.acquire(c, "A", now=1000, ttl_ms=10_000))
        # A foreign owner's release is a no-op — the live lease stays live.
        await db.write(lambda c: lease.release(c, "OTHER", epoch))
        blocked, _ = await db.write(lambda c: lease.acquire(c, "C", now=1500, ttl_ms=10_000))
        assert blocked is False
        # The true owner releases; now the lease is acquirable immediately.
        await db.write(lambda c: lease.release(c, "A", epoch))
        ok, e2 = await db.write(lambda c: lease.acquire(c, "C", now=1600, ttl_ms=10_000))
        assert ok and e2 == epoch + 1
    finally:
        await db.close()
