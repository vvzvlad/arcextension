import asyncio
import time

import pytest

from src.db.access import Database


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def test_write_failure_releases_lock_and_rolls_back(tmp_path):
    db = await _make_db(tmp_path)
    try:
        def bad(conn):
            conn.execute("INSERT INTO settings(key, value) VALUES ('a', '1')")
            raise RuntimeError("boom")  # fails partway through the tx

        with pytest.raises(RuntimeError):
            await db.write(bad)

        # The finally/except rollback must have released the write lock and left
        # the connection outside any transaction.
        assert db._conn.in_transaction is False

        # A SUBSEQUENT write must succeed — proof the lock did not leak.
        def good(conn):
            conn.execute("INSERT INTO settings(key, value) VALUES ('b', '2')")
            return "ok"

        assert await db.write(good) == "ok"

        # The failed insert was rolled back: only 'b' survived.
        rows = await db.read(
            lambda c: c.execute("SELECT key FROM settings ORDER BY key").fetchall()
        )
        assert rows == [("b",)]
    finally:
        await db.close()


async def test_write_commits_and_reader_sees_it(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.write(
            lambda c: c.execute("INSERT INTO settings(key, value) VALUES ('k', 'v')")
        )
        value = await db.read(
            lambda c: c.execute("SELECT value FROM settings WHERE key='k'").fetchone()
        )
        assert value == ("v",)
    finally:
        await db.close()


async def test_cancel_mid_write_does_not_leak_lock(tmp_path):
    # The central measured invariant (§4, 2026-07-26): a transaction that lives
    # entirely inside one thread call plays out and releases its lock even when the
    # awaiting task is cancelled — unlike a tx stretched across `await`, which would
    # hold the lock until close(). Guards against a future refactor slipping an
    # `await` into the write path (which would make this deadlock).
    db = await _make_db(tmp_path)
    try:
        def slow_write(conn):
            conn.execute("INSERT INTO settings(key, value) VALUES ('c', '1')")
            time.sleep(0.3)  # hold the writer thread so we can cancel mid-tx

        task = asyncio.create_task(db.write(slow_write))
        await asyncio.sleep(0.05)  # let the write get in flight on the writer thread
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The lock is released the moment CancelledError unwinds `async with`; the
        # uncancellable thread call plays out and commits. A subsequent write must
        # succeed (it queues behind the still-running _txn on the single worker) —
        # if the lock had leaked this would hang forever.
        def good(conn):
            conn.execute("INSERT INTO settings(key, value) VALUES ('d', '2')")

        await asyncio.wait_for(db.write(good), timeout=5)
        assert db._conn.in_transaction is False

        # The cancelled tx still committed ('c'), and the follow-up write ('d') too.
        rows = await db.read(
            lambda c: c.execute("SELECT key FROM settings ORDER BY key").fetchall()
        )
        assert ("c",) in rows and ("d",) in rows
    finally:
        await db.close()


async def test_reader_concurrency_during_write(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.write(
            lambda c: c.execute("INSERT INTO settings(key, value) VALUES ('x', '1')")
        )

        def slow_write(conn):
            conn.execute("INSERT INTO settings(key, value) VALUES ('y', '2')")
            time.sleep(0.3)  # hold the writer thread while readers run

        write_task = asyncio.create_task(db.write(slow_write))
        await asyncio.sleep(0.05)  # ensure the write is in flight

        def count(conn):
            return conn.execute("SELECT count(*) FROM settings").fetchone()[0]

        # Multiple concurrent readers must all complete (WAL) while the write is
        # still holding an open transaction — they never queue behind the writer.
        results = await asyncio.gather(*[db.read(count) for _ in range(5)])
        assert all(isinstance(r, int) for r in results)

        await write_task
        assert await db.read(count) == 2
    finally:
        await db.close()
