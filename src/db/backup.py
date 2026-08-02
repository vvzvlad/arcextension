"""Database backup: ``VACUUM INTO`` a temp copy, verify it, then atomically
rename it into place, keeping only the last N copies.

The temp file is removed in a ``finally`` on ANY failure (§12): a full disk that
kills the copy mid-write must never leave a DB-sized turd behind every night —
the "disk full" condition would otherwise amplify itself. ``VACUUM INTO`` fails
inside an open transaction, so a fresh short-lived connection with no open tx is
used. Rotation deletes the oldest copies only AFTER a new copy has passed its
integrity check.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

# How many successful backups to retain. Small constant on purpose (§12).
KEEP_LAST: int = 7

# Backup filenames: sortable timestamp with microseconds so several backups made
# within the same second never collide and still sort chronologically.
_TS_FORMAT = "%Y%m%dT%H%M%S_%f"
_PREFIX = "curator-"
_SUFFIX = ".db"
_TMP_SUFFIX = ".db.tmp"
_GLOB = f"{_PREFIX}*{_SUFFIX}"

# Explicit busy_timeout on the short-lived backup connection (never inherited).
_BUSY_TIMEOUT_MS = 5000


def _verify_backup(copy_path: Path) -> None:
    """Open the freshly written copy and prove it is usable.

    Runs ``PRAGMA integrity_check`` and a sanity row-count over the schema
    catalog. Raises on any problem. Kept as a module-level function so tests can
    monkeypatch it to force a post-write failure.
    """
    conn = sqlite3.connect(copy_path)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            raise RuntimeError(f"integrity_check failed: {rows!r}")
        # Sanity: a migrated DB always has schema objects; an empty/corrupt copy
        # would return 0 and we would rather fail than rotate a good copy away.
        (count,) = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if count <= 0:
            raise RuntimeError("sanity count returned no schema objects")
    finally:
        conn.close()


def _rotate(backup_dir: Path, keep: int) -> None:
    """Keep only the newest ``keep`` finished copies; delete the oldest."""
    copies = sorted(backup_dir.glob(_GLOB))  # timestamped names sort chronologically
    for stale in copies[:-keep] if keep > 0 else copies:
        try:
            stale.unlink()
        except OSError as exc:  # a failed rotation must not fail the backup
            logger.warning("backup rotation could not remove {}: {}", stale, exc)


def run_backup(db_path: str | Path, backup_dir: str | Path, keep: int = KEEP_LAST) -> Path:
    """Produce one verified backup copy of ``db_path`` in ``backup_dir``.

    Returns the path of the finished copy. The ``.tmp`` intermediate is removed in
    a ``finally`` on any failure so a partially written copy never survives.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime(_TS_FORMAT)
    final_path = backup_dir / f"{_PREFIX}{ts}{_SUFFIX}"
    tmp_path = backup_dir / f"{_PREFIX}{ts}{_TMP_SUFFIX}"

    try:
        # VACUUM INTO fails inside an open transaction — use a fresh short-lived
        # connection with no open tx. sqlite quotes the target as a string literal.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("VACUUM INTO ?", (str(tmp_path),))
        finally:
            conn.close()

        # Verify BEFORE the copy becomes the current backup and before rotation.
        _verify_backup(tmp_path)

        # Atomic rename: readers of backup_dir never see a half-written .db.
        tmp_path.replace(final_path)
    finally:
        # On ANY failure (VACUUM, verify, rename) drop the temp file. If the
        # rename already consumed it, this is a harmless no-op.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as exc:
                logger.warning("could not remove temp backup {}: {}", tmp_path, exc)

    # Only rotate after a new copy is finished and verified.
    _rotate(backup_dir, keep)
    logger.info("backup written: {}", final_path)
    return final_path


def _seconds_until_next_run(now: datetime, hour: int = 3, minute: int = 0) -> float:
    """Seconds from ``now`` to the next occurrence of ``hour:minute`` local time."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def nightly_backup_loop(db) -> None:
    """Sleep until ~03:00 local, run one backup, repeat.

    ``db`` exposes ``db_path`` and ``backup_dir``. Errors are logged and the loop
    continues (a single failed night must not kill the schedule). The success
    metric hook is a later phase; for now the outcome is just logged.
    """
    while True:
        delay = _seconds_until_next_run(datetime.now())
        logger.info("next backup in {:.0f}s", delay)
        await asyncio.sleep(delay)
        try:
            path = await asyncio.to_thread(run_backup, db.db_path, db.backup_dir)
            logger.info("nightly backup ok: {}", path)
        except Exception as exc:  # noqa: BLE001 - never let the schedule die
            logger.error("nightly backup failed: {}", exc)
