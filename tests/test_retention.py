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
    ones while anything a client could still be holding keeps de-duping its retry.

    Three markers straddle the horizon: ancient (swept), 20 days old (KEPT — this is the
    one that reddens if the window goes back to 7 days), and recent (kept). Remove the
    prune entirely and the ancient key survives, which reddens too."""
    db = await _make_db(tmp_path)
    try:
        now = 2_000 * _MS_PER_DAY

        def seed(c):
            # 45d — past any plausible claim, and past the 30d horizon -> pruned.
            record_idempotency_key(c, "ancient-batch", now - 45 * _MS_PER_DAY)
            # 20d — INSIDE the 30d horizon but OUTSIDE the old 7d one. A batch can sit
            # this long behind a repeatedly-extended pause (§7), so its marker must
            # survive; reverting the horizon to 7 days reddens exactly here.
            record_idempotency_key(c, "paused-batch", now - 20 * _MS_PER_DAY)
            # 1d — comfortably inside any window.
            record_idempotency_key(c, "recent-batch", now - 1 * _MS_PER_DAY)

        await db.write(seed)
        result = await db.write(
            lambda c: run_retention(c, now, ACTIONS_DAYS, JS_AUDIT_DAYS)
        )
        assert result.idempotency_keys_deleted == 1

        # Both still-claimable batches de-dupe their retry.
        assert await db.read(lambda c: idempotency_key_seen(c, "recent-batch")) is True
        assert await db.read(lambda c: idempotency_key_seen(c, "paused-batch")) is True
        # The ancient key is gone (its batch cannot still be pending).
        assert await db.read(lambda c: idempotency_key_seen(c, "ancient-batch")) is False

        # Non-idempotency settings rows are never touched by the sweep.
        remaining = await db.read(
            lambda c: sorted(
                r[0] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'qlkey:%'")
            )
        )
        assert remaining == ["qlkey:paused-batch", "qlkey:recent-batch"]
    finally:
        await db.close()


async def test_idempotency_horizon_outlives_an_extended_pause(tmp_path):
    """WHY the horizon is 30 days and not a week (§10 «оффлайн может длиться днями»).

    The upper bound on how long a client holds a claimed batch is not the network: while
    a pause is armed every flush answers 423, and a pause is extended by pressing the
    button again with no cap on repeats (§7 — only each individual pause is finite). Sweep
    the marker first and the batch is not dropped but RE-SENT, applying a second time —
    a duplicated ``reorder``, which is the very thing the Idempotency-Key exists to stop
    (the ops compose, they do not settle).

    Simulates that directly: a batch claimed before a fortnight of extended pause, whose
    retry lands 15 days later, must still be recognised. At the old 7-day horizon it is
    not, and the retry double-applies.
    """
    db = await _make_db(tmp_path)
    try:
        claimed_at = 2_000 * _MS_PER_DAY
        await db.write(lambda c: record_idempotency_key(c, "batch-held-through-pause",
                                                        claimed_at))

        # Retention keeps running daily throughout the pause.
        for day in range(1, 16):
            await db.write(
                lambda c, d=day: run_retention(
                    c, claimed_at + d * _MS_PER_DAY, ACTIONS_DAYS, JS_AUDIT_DAYS
                )
            )

        # The pause is lifted on day 15 and the client finally flushes its batch.
        assert await db.read(
            lambda c: idempotency_key_seen(c, "batch-held-through-pause")
        ) is True, "the retry would have applied a SECOND time"
    finally:
        await db.close()


def test_the_three_horizons_are_independent(tmp_path):
    # The idempotency change must not move `actions` / `js_audit`: each cutoff is
    # computed from its own window, so bumping one leaves the others exactly where they
    # were. Pure arithmetic on the same `now`, so it reddens if the cutoffs ever share
    # a horizon.
    from src.db.retention import _DEFAULT_IDEMPOTENCY_RETENTION_DAYS

    now = 1_000 * _MS_PER_DAY
    assert cutoff_ms(now, ACTIONS_DAYS) == now - ACTIONS_DAYS * _MS_PER_DAY
    assert cutoff_ms(now, JS_AUDIT_DAYS) == now - JS_AUDIT_DAYS * _MS_PER_DAY
    # js_audit stays the LONGEST (§12: the only trace of arbitrary code execution)…
    assert JS_AUDIT_DAYS > ACTIONS_DAYS > _DEFAULT_IDEMPOTENCY_RETENTION_DAYS
    # …and the idempotency window is long enough to outlive an extended pause.
    assert _DEFAULT_IDEMPOTENCY_RETENTION_DAYS >= 30
