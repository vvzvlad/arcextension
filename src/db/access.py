"""Async access layer over a single SQLite database.

The design is the one measured in docs/architecture.md §4 "Слой доступа к БД":

* **Writer** — ONE dedicated single-worker executor owning ONE connection created
  on its worker thread (``check_same_thread=False``, ``isolation_level=None`` so we
  drive BEGIN/COMMIT ourselves), serialized by an ``asyncio.Lock``. A write is one
  synchronous function run in one executor call — ``BEGIN IMMEDIATE`` … the caller's
  ``fn`` … ``COMMIT`` — with NO ``await`` inside. A measured tx that lives entirely
  inside one thread call finishes, commits and releases its lock even on task
  cancellation; a tx stretched across ``await`` holds the lock until ``close()``.
* **Readers** — each ``read`` opens its OWN short-lived connection on its OWN thread
  (via ``asyncio.to_thread``), so readers run concurrently with the writer under
  WAL instead of queueing behind it.
* Every connection sets ``busy_timeout`` explicitly (never inherited); WAL is
  enabled once at startup (DB-level, persistent).
"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

from loguru import logger

from src.db.migrations import MigrationResult, migrate

T = TypeVar("T")

# Explicit busy_timeout for every connection (never inherited).
DEFAULT_BUSY_TIMEOUT_MS = 5000


class Database:
    def __init__(self, db_path: str, backup_dir: str, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        self.db_path = db_path
        self.backup_dir = backup_dir
        self._busy_timeout_ms = busy_timeout_ms
        # Single-worker executor => every write runs on the same thread, so the one
        # writer connection is only ever touched by that thread.
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        # Populated by open().
        self.degraded: bool = False
        self.migration_reason: str | None = None
        self.migration_version: int = 0

    # --- lifecycle -----------------------------------------------------------
    async def open(self) -> MigrationResult:
        """Create the writer connection, enable WAL, then run migrations.

        A migration problem does NOT raise: it sets ``self.degraded`` and returns
        the result so the app can still serve /healthz (and later /metrics).
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._writer, self._connect_writer)
        result: MigrationResult = await loop.run_in_executor(self._writer, self._run_migrations)
        self.degraded = result.degraded
        self.migration_reason = result.reason
        self.migration_version = result.final_version
        if result.degraded:
            logger.warning("database opened in DEGRADED mode: {}", result.reason)
        else:
            logger.info("database opened at schema version {}", result.final_version)
        return result

    def _connect_writer(self) -> None:
        # Runs on the writer thread; the connection is owned by this thread.
        conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        # WAL is persistent (DB-level): set once here so readers run concurrently.
        conn.execute("PRAGMA journal_mode = WAL")
        self._conn = conn

    def _run_migrations(self) -> MigrationResult:
        assert self._conn is not None
        return migrate(self._conn, self.db_path, self.backup_dir)

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        if self._conn is not None:
            await loop.run_in_executor(self._writer, self._conn.close)
            self._conn = None
        self._writer.shutdown(wait=True)

    # --- writes --------------------------------------------------------------
    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` as one serialized write transaction.

        The whole transaction is a single synchronous call on the writer thread —
        no ``await`` inside — so it always commits-or-rolls-back and releases the
        lock, even if the awaiting task is cancelled.
        """
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(self._writer, self._txn, fn)

    def _txn(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        # Runs entirely on the writer thread. No await anywhere in here.
        conn = self._conn
        assert conn is not None, "Database.open() must be awaited before write()"
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            # Roll back on ANY failure (including a fn that raises partway) so the
            # write lock never leaks — a stuck lock on the singleton writer means
            # "for the life of the connection" = forever for the whole service.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            # Belt-and-suspenders: force the connection out of any open tx.
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

    # --- reads ---------------------------------------------------------------
    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` on a short-lived reader connection (own thread).

        Readers never touch the writer connection or lock — WAL lets them run
        concurrently with an in-flight write.
        """
        return await asyncio.to_thread(self._read, fn)

    def _read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            return fn(conn)
        finally:
            conn.close()
