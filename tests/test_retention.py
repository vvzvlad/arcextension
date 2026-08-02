"""Retention horizons (§12): actions vs js_audit have SEPARATE, longer windows.

The load-bearing property: a row old enough to be dropped from ``actions`` is
SPARED in ``js_audit`` because js_audit's horizon is longer. Seed one action and
one js_audit row at the SAME age (between the two horizons) and assert only the
action is deleted — swap/share the horizons and this reddens.
"""

from src.db.access import Database
from src.db.actions import insert_action
from src.db.audit import insert_js_audit
from src.db.quick_links import idempotency_key_seen, record_idempotency_key
from src.db.retention import cutoff_ms, run_retention

_MS_PER_DAY = 86_400_000
ACTIONS_DAYS = 90
JS_AUDIT_DAYS = 730


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


def test_cutoff_ms():
    now = 1_000 * _MS_PER_DAY
    assert cutoff_ms(now, 90) == now - 90 * _MS_PER_DAY


async def test_retention_horizons_spare_js_audit(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = 2_000 * _MS_PER_DAY  # a large, fixed "now" in ms

        def seed(c):
            # actions: one older than 90d (drop), one within 90d (keep), one at
            # 100d (older than actions horizon -> drop).
            insert_action(c, ts=now - 200 * _MS_PER_DAY, kind="dedupe_close",
                          status="done", initiator="curator", url="a")   # drop
            insert_action(c, ts=now - 10 * _MS_PER_DAY, kind="dedupe_close",
                          status="done", initiator="curator", url="b")   # keep
            insert_action(c, ts=now - 100 * _MS_PER_DAY, kind="dedupe_close",
                          status="done", initiator="curator", url="c")   # drop
            # js_audit: one at the SAME 100d age (spared, < 730d), one at 800d (drop).
            insert_js_audit(c, instance_id="i1", code="x", initiator="user",
                            now=now - 100 * _MS_PER_DAY)   # SPARED
            insert_js_audit(c, instance_id="i1", code="y", initiator="user",
                            now=now - 800 * _MS_PER_DAY)   # drop

        await db.write(seed)
        result = await db.write(
            lambda c: run_retention(c, now, ACTIONS_DAYS, JS_AUDIT_DAYS)
        )
        assert result.actions_deleted == 2   # the 200d and 100d rows
        assert result.js_audit_deleted == 1  # only the 800d row

        actions_left = await db.read(
            lambda c: sorted(r[0] for r in c.execute("SELECT url FROM actions"))
        )
        assert actions_left == ["b"]  # only the within-90d row survives

        js_left = await db.read(
            lambda c: [r[0] for r in c.execute("SELECT code FROM js_audit")]
        )
        # The 100d js_audit row is SPARED although a 100d ACTION was dropped —
        # this is the separate-horizon guarantee (§12).
        assert js_left == ["x"]
    finally:
        await db.close()


async def test_retention_prunes_old_idempotency_keys(tmp_path):
    """``qlkey:*`` markers grow ``settings`` unbounded; retention must sweep the OLD
    ones while a RECENT one still de-dupes a retry. Seed one old (beyond the 7d
    idempotency window) and one recent marker; run retention with the default
    idempotency horizon; assert only the old one is pruned and the recent one still
    reports ``idempotency_key_seen`` True. Widen the sweep to keep the old key (remove
    the prune) and this reddens."""
    db = await _make_db(tmp_path)
    try:
        now = 2_000 * _MS_PER_DAY

        def seed(c):
            # Recorded 30d ago (older than the 7d idempotency window) -> pruned.
            record_idempotency_key(c, "old-batch", now - 30 * _MS_PER_DAY)
            # Recorded 1d ago (inside the window) -> kept and still de-dupes.
            record_idempotency_key(c, "recent-batch", now - 1 * _MS_PER_DAY)

        await db.write(seed)
        result = await db.write(
            lambda c: run_retention(c, now, ACTIONS_DAYS, JS_AUDIT_DAYS)
        )
        assert result.idempotency_keys_deleted == 1

        # The recent key is still recorded, so a retry of THAT batch is a no-op.
        assert await db.read(lambda c: idempotency_key_seen(c, "recent-batch")) is True
        # The old key is gone (would re-apply, but the batch is long finished).
        assert await db.read(lambda c: idempotency_key_seen(c, "old-batch")) is False

        # Non-idempotency settings rows are never touched by the sweep.
        remaining = await db.read(
            lambda c: [
                r[0] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'qlkey:%'")
            ]
        )
        assert remaining == ["qlkey:recent-batch"]
    finally:
        await db.close()
