"""Direct (socket-free) tests of the one-transaction snapshot application and the
epoch-guarded disconnect write. These call the real sync ``fn(conn)`` bodies via
``Database.write`` so a mutation that removes a guard reddens the named test.
"""

import pytest

from src.db import queries
from src.db.access import Database
from src.ext.protocol import heartbeat_step
from src.ext.snapshot import apply_snapshot


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def _register(db, instance_id, session_id, now):
    return await db.write(
        lambda c: queries.hello_upsert(c, instance_id, session_id, "t", False, now)
    )


def _tab(tab_id, window_id=1, **over):
    t = {
        "tabId": tab_id,
        "windowId": window_id,
        "url": f"https://e/{tab_id}",
        "title": f"t{tab_id}",
        "favIconUrl": None,
        "pinned": False,
        "active": False,
        "audible": False,
        "ageMs": 1000,
        "openedAgoMs": 2000,
        "ageUnknown": False,
        "selfNavigating": False,
    }
    t.update(over)
    return t


async def _tab_ids(db, instance_id):
    rows = await db.read(
        lambda c: c.execute(
            "SELECT tab_id FROM tabs WHERE instance_id = ? ORDER BY tab_id",
            (instance_id,),
        ).fetchall()
    )
    return [r[0] for r in rows]


# --- sent_at-bounded delete (headline snapshot invariant) -------------------
async def test_snapshot_before_open_tab_does_not_delete_the_copy(tmp_path):
    db = await _make_db(tmp_path)
    try:
        base = 1_000_000
        epoch = await _register(db, "i", "s", base)

        # A phase-A copy inserted AFTER the request went out: updated_at = base.
        await db.write(
            lambda c: c.execute(
                "INSERT INTO tabs (instance_id, tab_id, window_id, opened_at, "
                "last_active_at, updated_at) VALUES ('i', 99, 1, ?, ?, ?)",
                (base, base, base),
            )
        )

        # Snapshot whose request was SENT earlier than the copy's write, and
        # whose tab list excludes the copy. The `updated_at <= sent_at` bound
        # must spare the copy (base > base-1000).
        snap = {"sessionId": "s", "focusedWindowId": 1,
                "tabs": [_tab(1), _tab(2)], "windows": []}
        await db.write(
            lambda c: apply_snapshot(
                c, "i", snap, sent_at=base - 1000, now=base, expected_epoch=epoch
            )
        )
        assert 99 in await _tab_ids(db, "i"), "the fresh copy must survive the bound"

        # A later snapshot whose request was sent AFTER the copy's write and that
        # still excludes it => now it is eligible and gets deleted.
        await db.write(
            lambda c: apply_snapshot(
                c, "i", snap, sent_at=base + 5000, now=base + 6000, expected_epoch=epoch
            )
        )
        assert 99 not in await _tab_ids(db, "i"), "an old excluded tab is deleted"
    finally:
        await db.close()


# --- session-change clear ---------------------------------------------------
async def test_session_id_change_clears_tabs(tmp_path):
    db = await _make_db(tmp_path)
    try:
        base = 2_000_000
        epoch = await _register(db, "i", "s1", base)

        snap1 = {"sessionId": "s1", "focusedWindowId": 1,
                 "tabs": [_tab(1), _tab(2)], "windows": []}
        await db.write(
            lambda c: apply_snapshot(c, "i", snap1, sent_at=base, now=base, expected_epoch=epoch)
        )
        assert await _tab_ids(db, "i") == [1, 2]

        # New session => browser restarted => all old tabs dropped. sent_at is set
        # EARLIER than tabs 1/2's updated_at so the time-bounded delete can NOT be
        # what removes them — only the session-change clear can.
        snap2 = {"sessionId": "s2", "focusedWindowId": 1,
                 "tabs": [_tab(3)], "windows": []}
        await db.write(
            lambda c: apply_snapshot(
                c, "i", snap2, sent_at=base - 1000, now=base + 100, expected_epoch=epoch
            )
        )
        assert await _tab_ids(db, "i") == [3], "session change must clear old tabs"
    finally:
        await db.close()


# --- UPSERT, never bare INSERT ----------------------------------------------
async def test_snapshot_upserts_over_a_preexisting_row(tmp_path):
    db = await _make_db(tmp_path)
    try:
        base = 3_000_000
        epoch = await _register(db, "i", "s", base)

        # Simulate a phase-A copy row already present for tab_id 1.
        await db.write(
            lambda c: c.execute(
                "INSERT INTO tabs (instance_id, tab_id, window_id, title, "
                "opened_at, last_active_at, updated_at) "
                "VALUES ('i', 1, 1, 'old', ?, ?, ?)",
                (base, base, base),
            )
        )

        snap = {"sessionId": "s", "focusedWindowId": 1,
                "tabs": [_tab(1, title="new")], "windows": []}
        # A bare INSERT would hit PRIMARY KEY(instance_id, tab_id) and abort here.
        await db.write(
            lambda c: apply_snapshot(c, "i", snap, sent_at=base, now=base + 10, expected_epoch=epoch)
        )
        # Applying the SAME snapshot again must also not raise (idempotent).
        await db.write(
            lambda c: apply_snapshot(c, "i", snap, sent_at=base, now=base + 20, expected_epoch=epoch)
        )

        rows = await db.read(
            lambda c: c.execute(
                "SELECT tab_id, title FROM tabs WHERE instance_id='i'"
            ).fetchall()
        )
        assert rows == [(1, "new")], "one row, updated in place (UPSERT)"
    finally:
        await db.close()


# --- epoch-guarded snapshot application (TOCTOU at eviction) ----------------
async def test_stale_epoch_snapshot_is_discarded(tmp_path):
    # A snapshot from a socket whose epoch was superseded (a newer hello bumped
    # conn_epoch) must be a no-op: it must not write tabs. Drop the epoch guard in
    # apply_snapshot and this reddens.
    db = await _make_db(tmp_path)
    try:
        base = 5_000_000
        e1 = await _register(db, "i", "s", base)
        e2 = await _register(db, "i", "s", base + 1)  # reconnect bumps the epoch
        assert e2 == e1 + 1
        snap = {"sessionId": "s", "focusedWindowId": 1,
                "tabs": [_tab(1)], "windows": []}
        # Applying with the STALE epoch e1 must be discarded.
        await db.write(
            lambda c: apply_snapshot(c, "i", snap, sent_at=base, now=base + 5, expected_epoch=e1)
        )
        assert await _tab_ids(db, "i") == [], "stale-epoch snapshot must not apply"
        # The current epoch e2 applies normally.
        await db.write(
            lambda c: apply_snapshot(c, "i", snap, sent_at=base + 10, now=base + 15, expected_epoch=e2)
        )
        assert await _tab_ids(db, "i") == [1]
    finally:
        await db.close()


# --- malformed tabId is skipped, not fatal ----------------------------------
async def test_invalid_tab_id_is_skipped_not_fatal(tmp_path):
    # A tab with a missing/non-int tabId must be skipped, not abort the whole
    # transaction (which would knock the instance out of curation). Valid tabs in
    # the same snapshot still apply.
    db = await _make_db(tmp_path)
    try:
        base = 6_000_000
        epoch = await _register(db, "i", "s", base)
        snap = {"sessionId": "s", "focusedWindowId": 1,
                "tabs": [_tab(1), _tab(None), _tab("x"), _tab(2)], "windows": []}
        await db.write(
            lambda c: apply_snapshot(c, "i", snap, sent_at=base, now=base + 5, expected_epoch=epoch)
        )
        assert await _tab_ids(db, "i") == [1, 2], "bad tabId skipped, valid tabs applied"
    finally:
        await db.close()


# --- epoch-guarded disconnect write (THE headline invariant) ----------------
async def test_late_disconnect_does_not_clobber_newer_connection(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = 4_000_000
        e1 = await _register(db, "i", "s", now)            # first connection
        e2 = await _register(db, "i", "s", now + 1)        # reconnect: epoch bumps
        assert e2 == e1 + 1

        # The OLD socket's finalizer runs LATE with its own (stale) epoch e1.
        await db.write(lambda c: queries.mark_disconnected(c, "i", e1))

        connected = await db.read(
            lambda c: c.execute(
                "SELECT connected FROM instances WHERE id='i'"
            ).fetchone()[0]
        )
        # The e1-guarded UPDATE matches nothing (current epoch is e2) => the live
        # newer connection stays connected. Drop `AND conn_epoch=?` and this is 0.
        assert connected == 1
    finally:
        await db.close()


# --- heartbeat miss decision (pure function) --------------------------------
def test_heartbeat_two_consecutive_misses_disconnects():
    # A received pong resets the counter, never disconnects.
    assert heartbeat_step(alive=True, misses=1) == (0, False)
    # First miss: counted, not yet a disconnect.
    assert heartbeat_step(alive=False, misses=0) == (1, False)
    # Second consecutive miss: disconnect.
    assert heartbeat_step(alive=False, misses=1) == (2, True)
