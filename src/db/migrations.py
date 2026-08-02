"""Migration runner keyed on ``PRAGMA user_version``.

Contract (every clause is measured — see docs/architecture.md §4 "Слой доступа к
БД"; measured 2026-08-02 on python:3.11-slim = Python 3.11.15 / libsqlite3
3.46.1):

* Before the first step, take a ``VACUUM INTO`` safety copy (only when there is
  existing schema to protect — a version-0 DB has nothing to lose).
* Each step runs under an explicit ``BEGIN IMMEDIATE``. ``PRAGMA user_version`` is
  re-read INSIDE that transaction and the step is applied ONLY if the version is
  still behind — so two containers of a rolling redeploy never re-apply a step and
  crash-loop on "duplicate column". DDL + the ``user_version`` bump commit
  together; a rollback undoes both.
* A ``user_version`` GREATER than ``MAX_VERSION`` means the service rolled back
  onto a DB migrated by newer code: enter degraded mode, do not touch the schema.
* Any failure rolls back the step and enters degraded mode; the runner never
  crashes startup (§4/§12: degraded serves /healthz + /metrics, refuses /ext and
  mutating /api/*).
"""

import sqlite3
from dataclasses import dataclass

from loguru import logger

from src.db import backup
from src.db.schema import MAX_VERSION, STEPS

# Explicit busy_timeout so a concurrent runner's BEGIN IMMEDIATE waits for the
# other's write lock instead of failing instantly (never inherited).
_BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class MigrationResult:
    ok: bool
    degraded: bool
    reason: str | None
    final_version: int


def _current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(
    conn: sqlite3.Connection,
    db_path: str,
    backup_dir: str,
    steps: list[tuple[int, list[str]]] | None = None,
    max_version: int | None = None,
) -> MigrationResult:
    """Run pending migration steps on ``conn`` (isolation_level=None expected).

    ``conn`` must be the dedicated writer connection. ``db_path``/``backup_dir``
    are used for the pre-migration safety copy. Returns a :class:`MigrationResult`;
    it never raises for a migration problem — it reports degraded instead.
    """
    # Sentinel defaults (never a mutable module-level list as a default arg).
    steps = STEPS if steps is None else steps
    max_version = MAX_VERSION if max_version is None else max_version

    # Explicit busy_timeout — guarantees the rolling-redeploy contract regardless
    # of how the connection was created.
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")

    start_version = _current_version(conn)

    # Service rolled back onto a DB migrated by newer code: refuse to touch it.
    if start_version > max_version:
        reason = (
            f"user_version {start_version} > max known {max_version}: "
            "service rolled back onto a newer DB"
        )
        logger.error("migration degraded: {}", reason)
        return MigrationResult(ok=False, degraded=True, reason=reason, final_version=start_version)

    # Safety copy before the first mutation — ONLY when a step will actually run.
    # A version-0 DB has no schema worth protecting; a DB already at max_version
    # applies no step, so a copy there would run a full VACUUM INTO on EVERY boot,
    # delaying /healthz behind Traefik and evicting nightly copies from KEEP_LAST.
    if 0 < start_version < max_version:
        try:
            path = backup.run_backup(db_path, backup_dir)
            logger.info("pre-migration safety copy: {}", path)
        except Exception as exc:  # noqa: BLE001 - cannot migrate without a safety net
            reason = f"pre-migration backup failed: {exc}"
            logger.error("migration degraded: {}", reason)
            return MigrationResult(
                ok=False, degraded=True, reason=reason, final_version=start_version
            )

    for target, statements in steps:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Re-read INSIDE the transaction: BEGIN IMMEDIATE gives mutual
            # exclusion from here, so a concurrent runner that already applied this
            # step has committed a higher version we now observe and skip.
            inside_version = _current_version(conn)
            if inside_version < target:
                for stmt in statements:
                    conn.execute(stmt)
                # PRAGMA cannot be parameterized; target is our own trusted int.
                conn.execute(f"PRAGMA user_version = {target}")
                logger.info("applied migration step -> version {}", target)
            else:
                logger.info(
                    "migration step {} already applied (version {}), skipping",
                    target,
                    inside_version,
                )
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - degrade, never crash startup
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            reason = f"migration step {target} failed: {exc}"
            logger.error("migration degraded: {}", reason)
            return MigrationResult(
                ok=False, degraded=True, reason=reason, final_version=_current_version(conn)
            )

    final_version = _current_version(conn)
    logger.info("migrations complete at version {}", final_version)
    return MigrationResult(ok=True, degraded=False, reason=None, final_version=final_version)
