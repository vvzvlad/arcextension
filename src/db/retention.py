"""Periodic retention: drop old ``actions`` and old ``js_audit`` rows (§12).

Two SEPARATE horizons, and ``js_audit``'s is deliberately the LONGER one: it is
the only trace of arbitrary code execution and must not be swept away with the
routine ``dedupe_close`` noise in ``actions`` (§12). The deletes are pure
``fn(conn)`` bodies so they are unit-tested against seeded old/new ``ts`` rows;
the sleep loop is thin and not unit-tested.

Every delete runs inside one ``Database.write`` transaction (no ``await``).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass

from loguru import logger

from src.db.quick_links import prune_idempotency_keys

_MS_PER_DAY = 86_400_000

# How often the retention task wakes. A day is plenty — retention is not
# time-critical, and a coarse period keeps it off the hot path.
_RETENTION_INTERVAL_S = 24 * 60 * 60

# Idempotency markers (``qlkey:*``) guard a client's retry of a still-pending offline
# flush (§10). The horizon has to outlive the LONGEST a claimed batch can stay
# unconfirmed — and the real upper bound is not the network.
#
# A week looks generous against "offline can last days" (§10) until the pause is taken
# into account: while a pause is armed EVERY flush answers 423, and a pause is extended
# by simply pressing the button again, with no cap on how many times (§7 — only each
# individual pause is finite). So the client can legitimately sit on one claimed batch,
# key and all, for far longer than a week. Sweeping the marker first is not a harmless
# cleanup: the batch is not dropped, it is RE-SENT, and with the marker gone it applies a
# SECOND time — a duplicated ``reorder``, exactly the outcome this whole mechanism
# exists to prevent, and the ops are NOT self-idempotent (``reorder`` and ``add`` both
# compose rather than settle).
#
# 30 days is chosen to swallow a realistically-forgotten pause plus a long absence, and
# it costs nothing: one short ``settings`` row per FLUSH (not per op), pruned by age.
# Even a daily flusher leaves ~30 rows. The horizon stays well inside ``actions``' own
# (90 days by default), so this is not a new storage class either.
_DEFAULT_IDEMPOTENCY_RETENTION_DAYS = 30


def cutoff_ms(now_ms: int, retention_days: int) -> int:
    """The ``ts`` boundary: rows strictly older than this are eligible."""
    return now_ms - retention_days * _MS_PER_DAY


def delete_old_actions(conn: sqlite3.Connection, cutoff: int) -> int:
    """Delete ``actions`` rows with ``ts < cutoff``; return the count deleted."""
    cur = conn.execute("DELETE FROM actions WHERE ts < ?", (cutoff,))
    return cur.rowcount


def delete_old_js_audit(conn: sqlite3.Connection, cutoff: int) -> int:
    """Delete ``js_audit`` rows with ``ts < cutoff``; return the count deleted."""
    cur = conn.execute("DELETE FROM js_audit WHERE ts < ?", (cutoff,))
    return cur.rowcount


@dataclass(frozen=True)
class RetentionResult:
    actions_deleted: int
    js_audit_deleted: int
    idempotency_keys_deleted: int = 0


def run_retention(
    conn: sqlite3.Connection,
    now_ms: int,
    actions_retention_days: int,
    js_audit_retention_days: int,
    idempotency_retention_days: int = _DEFAULT_IDEMPOTENCY_RETENTION_DAYS,
) -> RetentionResult:
    """Apply ALL horizons in one transaction; return the per-table counts.

    Each cutoff is computed independently — ``js_audit`` uses its own, longer window —
    so a row old enough to drop from ``actions`` survives in ``js_audit`` (§12). The
    ``qlkey:*`` idempotency markers get their own THIRD window, sized to outlive an
    indefinitely-extended pause rather than a network outage (see the constant above).
    Three windows, three independent cutoffs: changing one never moves another. This
    single ``fn(conn)`` is what the app's periodic task hands to ``Database.write``.
    """
    a = delete_old_actions(conn, cutoff_ms(now_ms, actions_retention_days))
    j = delete_old_js_audit(conn, cutoff_ms(now_ms, js_audit_retention_days))
    k = prune_idempotency_keys(conn, cutoff_ms(now_ms, idempotency_retention_days))
    return RetentionResult(actions_deleted=a, js_audit_deleted=j, idempotency_keys_deleted=k)


async def retention_loop(
    db,
    actions_retention_days: int,
    js_audit_retention_days: int,
    idempotency_retention_days: int = _DEFAULT_IDEMPOTENCY_RETENTION_DAYS,
) -> None:
    """Sibling to the nightly backup loop: run retention once, then every day.

    A single failed run is logged and the schedule continues (a broken retention
    must not kill the process). The sleep loop is intentionally trivial — the
    deletion logic under test is :func:`run_retention`.
    """
    while True:
        try:
            now_ms = int(time.time() * 1000)
            result = await db.write(
                lambda c: run_retention(
                    c, now_ms, actions_retention_days, js_audit_retention_days,
                    idempotency_retention_days,
                )
            )
            logger.info(
                "retention: deleted {} actions, {} js_audit rows, {} idempotency keys",
                result.actions_deleted,
                result.js_audit_deleted,
                result.idempotency_keys_deleted,
            )
        except Exception as exc:  # noqa: BLE001 - never let the schedule die
            logger.error("retention run failed: {}", exc)
        await asyncio.sleep(_RETENTION_INTERVAL_S)
