"""rules access layer + validation (§8): insert validation, stored-then-uncompilable
=> invalid=1 excluded, and revalidate_rules orphaning a retired instance's rules."""

import sqlite3

import pytest

from src.db.access import Database
from src.rules import access
from src.rules.matcher import InvalidPattern, best_match


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


def _seed_instance(conn, iid):
    conn.execute("INSERT INTO instances (id) VALUES (?)", (iid,))


async def test_insert_rejects_full_url_pattern(tmp_path):
    db = await _make_db(tmp_path)
    try:
        # A full URL is not the hostPattern[:port] grammar => rejected at save (§8).
        with pytest.raises(InvalidPattern):
            await db.write(
                lambda c: access.insert_rule(
                    c, pattern="https://borneo.lc/path", instance_id="home", created_at=1
                )
            )
        # Nothing was written.
        rows = await db.read(access.list_rules)
        assert rows == []
    finally:
        await db.close()


async def test_stored_uncompilable_excluded_and_revalidated_invalid(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: _seed_instance(c, "home"))
        # Insert a BAD pattern directly (bypassing the save gate) to model a rule
        # that stopped compiling after storage.
        def _seed_bad(c):
            c.execute(
                "INSERT INTO rules (pattern, instance_id, invalid, created_at) "
                "VALUES ('https://x/y', 'home', 0, 1)"
            )
            return c.execute("SELECT id FROM rules").fetchone()[0]

        bad_id = await db.write(_seed_bad)

        # Excluded from matching immediately (defensive), even before revalidation.
        rules = await db.read(access.list_rules)
        assert best_match("https://x/y", rules) is None

        # revalidate flips it to invalid=1 (counted, surfaced) and reports 1 change.
        changed = await db.write(
            lambda c: access.revalidate_rules(c, {"home"})
        )
        assert changed == 1
        row = await db.read(lambda c: access.get_rule(c, bad_id))
        assert row["invalid"] == 1
        # A second revalidation is a no-op (idempotent).
        assert await db.write(lambda c: access.revalidate_rules(c, {"home"})) == 0
    finally:
        await db.close()


async def test_revalidate_marks_orphaned_instance_rules_invalid(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: _seed_instance(c, "home"))
        rid = await db.write(
            lambda c: access.insert_rule(
                c, pattern="borneo.lc", instance_id="home", created_at=1
            )
        )
        assert (await db.read(lambda c: access.get_rule(c, rid)))["invalid"] == 0

        # Retire the instance: it is gone from the known set => its rule orphans.
        # (This is the "delete instance -> rules invalid on next pass" acceptance.)
        changed = await db.write(lambda c: access.revalidate_rules(c, set()))
        assert changed == 1
        assert (await db.read(lambda c: access.get_rule(c, rid)))["invalid"] == 1
        assert await db.read(access.count_invalid) == 1

        # The instance comes back => the rule is valid again (flag cleared).
        changed_back = await db.write(lambda c: access.revalidate_rules(c, {"home"}))
        assert changed_back == 1
        assert (await db.read(lambda c: access.get_rule(c, rid)))["invalid"] == 0
    finally:
        await db.close()


async def test_update_clears_invalid_and_delete_removes(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: _seed_instance(c, "home"))
        rid = await db.write(
            lambda c: access.insert_rule(
                c, pattern="borneo.lc", instance_id="home", created_at=1
            )
        )
        # Force invalid, then a valid update clears it.
        await db.write(lambda c: c.execute("UPDATE rules SET invalid=1 WHERE id=?", (rid,)))
        await db.write(
            lambda c: access.update_rule(
                c, rid, pattern="borneo.lc:443", instance_id="home", singleton=True
            )
        )
        row = await db.read(lambda c: access.get_rule(c, rid))
        assert row["invalid"] == 0 and row["singleton"] == 1 and row["pattern"] == "borneo.lc:443"

        await db.write(lambda c: access.delete_rule(c, rid))
        assert await db.read(access.count_rules) == 0
    finally:
        await db.close()
