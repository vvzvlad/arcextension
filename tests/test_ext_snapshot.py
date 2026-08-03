"""Direct (socket-free) tests of the one-transaction snapshot application and the
epoch-guarded disconnect write. These call the real sync ``fn(conn)`` bodies via
``Database.write`` so a mutation that removes a guard reddens the named test.
"""

import pytest

from src.db import queries
from src.db.access import Database
from src.ext.protocol import heartbeat_step
from src.ext.snapshot import apply_snapshot


# The pending-request TTL these tests hand to `upsert_enroll_request_capped`. Wide enough
# that every row in the tests below is LIVE, so the TTL branch only fires where a test
# deliberately reaches past it (see the expired-row test).
_TTL_MS = 60 * 60_000


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def _register(db, instance_id, session_id, now):
    # Enrollment (issue #35): hello_upsert is UPDATE-only and only bumps an ALREADY
    # active row (a hello never creates one). Create the approved row first — the thing
    # Task E's operator approval does — so the first hello returns conn_epoch=1 and a
    # reconnect bumps it to 2. ON CONFLICT DO NOTHING so a repeat _register (reconnect)
    # reuses the same row and its bumped epoch.
    await db.write(
        lambda c: c.execute(
            "INSERT INTO instances (id, status, secret_hash, connected, conn_epoch) "
            "VALUES (?, 'active', ?, 0, 0) ON CONFLICT(id) DO NOTHING",
            (instance_id, f"hash-{instance_id}"),
        )
    )
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


# --- malformed fields must not abort the whole transaction ------------------
async def test_malformed_ages_and_windows_do_not_abort(tmp_path):
    # Non-numeric ages coerce to 0 (fresh); a bad/duplicate window is skipped.
    # Without this, int("x") / a NOT-NULL / a PK collision aborts apply_snapshot
    # and drops the instance out of curation.
    db = await _make_db(tmp_path)
    try:
        base = 7_000_000
        epoch = await _register(db, "i", "s", base)
        snap = {
            "sessionId": "s", "focusedWindowId": 1,
            "tabs": [_tab(1, ageMs="x"), _tab(2, openedAgoMs=[])],  # malformed ages
            "windows": [
                {"id": 1, "type": "normal", "state": "normal"},
                {"id": None, "type": "normal"},   # bad id -> skipped
                {"id": 2},                          # missing type -> skipped
                {"id": 1, "type": "normal"},        # duplicate window_id -> skipped
            ],
        }
        await db.write(
            lambda c: apply_snapshot(c, "i", snap, sent_at=base, now=base + 5, expected_epoch=epoch)
        )
        assert await _tab_ids(db, "i") == [1, 2]   # both tabs applied
        wins = await db.read(
            lambda c: c.execute(
                "SELECT window_id FROM windows WHERE instance_id='i'"
            ).fetchall()
        )
        assert wins == [(1,)]                        # only the one valid window
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


async def test_snapshot_session_id_obeys_the_same_rule_as_hello(tmp_path):
    """``instances.session_id`` has TWO writers; the invariant must bind both.

    The hello path refuses a non-str / >200-char ``sessionId`` outright, on the grounds
    that the mirror compares the value VERBATIM (a relocation stays live only while both
    endpoints' sessions match), so it may never be truncated or coerced. That is a claim
    about the COLUMN — and `apply_snapshot` rewrites the very same column on every pass,
    where it used to accept anything the frame carried.

    Here the frame is already authenticated and there is no socket to reject from inside a
    write transaction, so an unusable value is SKIPPED (the rule the tab/window loops
    follow). Skipping specifically must not read as "the session changed": that would wipe
    every tab of a live instance on one malformed frame. Reddens if the checks are dropped,
    or if a bad value is allowed to trigger the session-change clear.
    """
    db = await _make_db(tmp_path)
    try:
        base = 6_500_000
        epoch = await _register(db, "i", "s1", base)
        good = {"sessionId": "s1", "focusedWindowId": 1,
                "tabs": [_tab(1), _tab(2)], "windows": []}
        await db.write(
            lambda c: apply_snapshot(c, "i", good, sent_at=base, now=base, expected_epoch=epoch)
        )
        assert await _tab_ids(db, "i") == [1, 2]

        async def stored_session():
            return await db.read(
                lambda c: c.execute(
                    "SELECT session_id FROM instances WHERE id='i'"
                ).fetchone()[0]
            )

        # A non-str and an over-ceiling session id are both unusable. sent_at is set well
        # BEFORE the tabs' updated_at so the bounded delete cannot be what keeps them —
        # only the absence of a session-change clear can.
        for bad in ({"a": 1}, 12345, "x" * 201):
            snap = {"sessionId": bad, "focusedWindowId": 1, "tabs": [], "windows": []}
            await db.write(
                lambda c, s=snap: apply_snapshot(
                    c, "i", s, sent_at=base - 1000, now=base + 100, expected_epoch=epoch
                )
            )
            assert await stored_session() == "s1", f"{bad!r} must not overwrite the session"
            assert await _tab_ids(db, "i") == [1, 2], f"{bad!r} must not wipe the tabs"

        # A str at exactly the ceiling IS a session id — and so is None ("no session").
        # Both are real changes and clear the tabs, which is what proves the guard above
        # is a validity check and not a blanket "never change the session".
        at_ceiling = "y" * 200
        await db.write(
            lambda c: apply_snapshot(
                c, "i", {"sessionId": at_ceiling, "focusedWindowId": 1, "tabs": [], "windows": []},
                sent_at=base - 1000, now=base + 200, expected_epoch=epoch,
            )
        )
        assert await stored_session() == at_ceiling
        assert await _tab_ids(db, "i") == []
    finally:
        await db.close()


async def test_snapshot_focused_window_id_is_not_fatal_when_malformed(tmp_path):
    # focused_window_id is an INTEGER column. A dict/list reaches sqlite3 as a parameter it
    # cannot adapt and raises InterfaceError INSIDE the write — aborting the whole snapshot
    # and dropping a live instance out of curation until it reconnects, which is exactly
    # the failure mode the tab/window loops are written to avoid. Unusable => NULL.
    db = await _make_db(tmp_path)
    try:
        base = 6_600_000
        epoch = await _register(db, "i", "s", base)
        for bad in ({"w": 1}, ["w"], "3", True):
            snap = {"sessionId": "s", "focusedWindowId": bad,
                    "tabs": [_tab(1)], "windows": []}
            await db.write(
                lambda c, s=snap: apply_snapshot(
                    c, "i", s, sent_at=base, now=base + 5, expected_epoch=epoch
                )
            )
            assert await db.read(
                lambda c: c.execute(
                    "SELECT focused_window_id FROM instances WHERE id='i'"
                ).fetchone()[0]
            ) is None, f"{bad!r} must land as NULL"
            # The rest of the snapshot still applied — the transaction was not aborted.
            assert await _tab_ids(db, "i") == [1]

        # A real window id is still stored.
        await db.write(
            lambda c: apply_snapshot(
                c, "i", {"sessionId": "s", "focusedWindowId": 7, "tabs": [_tab(1)],
                         "windows": []},
                sent_at=base, now=base + 6, expected_epoch=epoch,
            )
        )
        assert await db.read(
            lambda c: c.execute(
                "SELECT focused_window_id FROM instances WHERE id='i'"
            ).fetchone()[0]
        ) == 7
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


# --- UPDATE-only upsert / rejection invariants (§3, issue #35) ---------------
async def test_hello_upsert_is_update_only_and_returns_none_for_missing_row(tmp_path):
    # A hello for an id with NO instances row creates nothing and returns None (never
    # int(None)). Reverting _HELLO_UPSERT to its INSERT branch reddens the COUNT check.
    db = await _make_db(tmp_path)
    try:
        epoch = await db.write(
            lambda c: queries.hello_upsert(c, "ghost", "s", "t", False, 1)
        )
        assert epoch is None
        count = await db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
        )
        assert count == 0
    finally:
        await db.close()


async def test_hello_upsert_ignores_a_revoked_row(tmp_path):
    # status='active' guard: a hello must not bump a revoked row (returns None, row
    # untouched). Dropping the guard would silently re-activate a revoked instance.
    db = await _make_db(tmp_path)
    try:
        await db.write(
            lambda c: c.execute(
                "INSERT INTO instances (id, status, connected, conn_epoch) "
                "VALUES ('r', 'revoked', 0, 5)"
            )
        )
        epoch = await db.write(
            lambda c: queries.hello_upsert(c, "r", "s", "t", False, 1)
        )
        assert epoch is None
        row = await db.read(
            lambda c: c.execute(
                "SELECT connected, conn_epoch, status FROM instances WHERE id='r'"
            ).fetchone()
        )
        assert row == (0, 5, "revoked")
    finally:
        await db.close()


async def test_record_rejection_is_update_only_no_row_created(tmp_path):
    # A rejection for an unknown id is a silent no-op — the anti-flood point of §3.
    db = await _make_db(tmp_path)
    try:
        await db.write(
            lambda c: queries.record_rejection(c, "ghost", "protocol", 1)
        )
        count = await db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
        )
        assert count == 0
    finally:
        await db.close()


async def test_resolve_secret_hashes_raw_and_maps_to_id_and_status(tmp_path):
    # Option A: resolve_secret takes the RAW secret and hashes it internally, matching the
    # stored sha256. A row storing sha256(raw) resolves when presented the RAW secret.
    db = await _make_db(tmp_path)
    raw = "raw-secret-a"
    try:
        await db.write(
            lambda c: c.execute(
                "INSERT INTO instances (id, status, secret_hash) VALUES ('a', 'active', ?)",
                (queries.sha256_hex(raw),),
            )
        )
        assert await db.read(lambda c: queries.resolve_secret(c, raw)) == ("a", "active")
        assert await db.read(lambda c: queries.resolve_secret(c, "nope")) is None
    finally:
        await db.close()


async def test_resolve_secret_db_leak_resistance(tmp_path):
    # DB-leak-resistance (option A): the DB stores only sha256(raw). Presenting the RAW
    # secret authenticates; presenting the STORED sha256 value (what a DB leak would hand
    # an attacker) hashes to something else and matches NOTHING — so a hash leak is not a
    # usable credential.
    db = await _make_db(tmp_path)
    raw = "raw-secret-leak"
    stored_hash = queries.sha256_hex(raw)
    try:
        await db.write(
            lambda c: c.execute(
                "INSERT INTO instances (id, status, secret_hash) VALUES ('leak', 'active', ?)",
                (stored_hash,),
            )
        )
        # The raw secret authenticates (server hashes it to the stored value).
        assert await db.read(lambda c: queries.resolve_secret(c, raw)) == ("leak", "active")
        # The stored hash presented AS the secret does not (it hashes to a different value).
        assert await db.read(lambda c: queries.resolve_secret(c, stored_hash)) is None
    finally:
        await db.close()


async def test_upsert_enroll_request_does_not_bump_first_seen_at(tmp_path):
    # first_seen_at is frozen across repeats (so the TTL is reachable); last_seen_at /
    # title / origin refresh to the latest. secret_hash is frozen TOO — see
    # test_pending_request_credential_is_frozen_against_substitution for why.
    db = await _make_db(tmp_path)
    try:
        await db.write(
            lambda c: queries.upsert_enroll_request(
                c, "u1", "o1", "T1", 1, "h1", now=100
            )
        )
        await db.write(
            lambda c: queries.upsert_enroll_request(
                c, "u1", "o2", "T2", 1, "h2", now=200
            )
        )
        row = await db.read(
            lambda c: c.execute(
                "SELECT first_seen_at, last_seen_at, suggested_title, secret_hash, origin "
                "FROM enroll_requests WHERE install_uuid='u1'"
            ).fetchone()
        )
        assert row == (100, 200, "T2", "h1", "o2")
        count = await db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()[0]
        )
        assert count == 1
    finally:
        await db.close()


async def test_pending_request_credential_is_frozen_against_substitution(tmp_path):
    """THE TOCTOU on the pending list: the operator must approve the credential they SAW.

    Attack shape: the row is keyed on ``install_uuid``, and the enroll gate only requires
    the (shared, short-lived) window code — so anyone who knows a victim's install_uuid and
    the current code could re-file that request with THEIR secret. Every operator-visible
    field (origin, suggested_title, protocol_version, the first-8 uuid) can be left
    identical, so the console row does not change at all and the click enrolls the
    attacker's credential under the victim's identity.

    The fix is that the stored credential is immutable for the life of the row: a repeat
    with a DIFFERENT hash writes nothing at all — not the hash, and not last_seen_at, so
    the stale row still ages out on its original schedule and the honest client's next
    attempt is accepted. Reddens if ``secret_hash = excluded.secret_hash`` returns to the
    ON CONFLICT clause, or if the mismatch stops being reported to the caller.
    """
    db = await _make_db(tmp_path)
    try:
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "victim", "o", "Laptop", 1, "victim-hash", 100, 8, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED

        # The substitution attempt: same uuid, same visible fields, different secret.
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "victim", "o", "Laptop", 1, "attacker-hash", 200, 8, _TTL_MS)
        ) == queries.ENROLL_SECRET_MISMATCH
        assert await db.read(
            lambda c: c.execute(
                "SELECT secret_hash, last_seen_at FROM enroll_requests "
                "WHERE install_uuid='victim'"
            ).fetchone()
        ) == ("victim-hash", 100)

        # The honest repeat (the client re-sends the secret it persisted) still refreshes
        # everything it is allowed to refresh — the freeze is not a freeze of the row.
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "victim", "o2", "Renamed", 1, "victim-hash", 300, 8, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED
        assert await db.read(
            lambda c: c.execute(
                "SELECT last_seen_at, suggested_title, origin FROM enroll_requests "
                "WHERE install_uuid='victim'"
            ).fetchone()
        ) == (300, "Renamed", "o2")

        # And once the row is gone (operator rejected it, or the TTL sweep took it), the
        # new secret enrolls normally — the freeze self-heals, it does not brick a uuid.
        await db.write(lambda c: queries.reject_enroll_request(c, "victim"))
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "victim", "o", "Laptop", 1, "fresh-hash", 400, 8, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED
    finally:
        await db.close()


async def test_expired_pending_row_does_not_freeze_the_credential(tmp_path):
    """The credential freeze must follow the SAME TTL both read surfaces apply.

    `list_pending_enroll_requests` and `get_enroll_request` filter on
    ``first_seen_at >= now - ttl_ms``; the physical sweep only catches up within a tick.
    A row inside that gap is invisible in /admin and unapprovable — yet the freeze used to
    consult it anyway and answer ``secret_conflict`` to a legitimate re-registration,
    leaving the operator with nothing to reject and the client with no way through. An
    expired row must therefore read as ABSENT: replaced outright, new secret, new
    first_seen_at. Reddens if the TTL filter is dropped from the lookup, or if the replace
    degrades to the plain UPSERT (which leaves secret_hash and first_seen_at alone).
    """
    db = await _make_db(tmp_path)
    try:
        t0 = 1_000_000
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "u1", "o", "Laptop", 1, "old-hash", t0, 8, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED

        # Exactly ON the cutoff the row is still LIVE (the readers compare with `>=`), so
        # the freeze is untouched for rows an operator can still act on.
        live = t0 + _TTL_MS
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "u1", "o", "Laptop", 1, "new-hash", live, 8, _TTL_MS)
        ) == queries.ENROLL_SECRET_MISMATCH

        # One ms past it the row is expired — gone for every reader — so the new secret
        # goes in and the row restarts its TTL from this request.
        past = t0 + _TTL_MS + 1
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "u1", "o2", "Renamed", 1, "new-hash", past, 8, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED
        assert await db.read(
            lambda c: c.execute(
                "SELECT secret_hash, first_seen_at, last_seen_at, suggested_title "
                "FROM enroll_requests WHERE install_uuid='u1'"
            ).fetchone()
        ) == ("new-hash", past, past, "Renamed")
        # Exactly one row — the expired one was replaced, not duplicated.
        assert await db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()[0]
        ) == 1
    finally:
        await db.close()


async def test_capacity_count_is_not_ttl_filtered(tmp_path):
    """The ceiling counts ROWS, expired or not — deliberately stricter than the readers.

    The anti-flood bound exists to keep the table (and the operator's list) from growing
    without bound; a row that is expired but not yet swept is still a row. TTL-filtering
    this count would let a flood hold `max_pending` live rows AND an unbounded tail of
    expired ones between sweeps. The refusal it produces is self-clearing — the sweeper
    runs every TICK_MS — unlike the credential freeze above. Reddens if a `WHERE
    first_seen_at >= ?` is added to the COUNT.
    """
    db = await _make_db(tmp_path)
    try:
        t0 = 1_000_000
        for uuid, h in (("u1", "h1"), ("u2", "h2")):
            assert await db.write(
                lambda c, u=uuid, hh=h: queries.upsert_enroll_request_capped(
                    c, u, None, None, 1, hh, t0, 2, _TTL_MS)
            ) == queries.ENROLL_ACCEPTED
        # Both rows are now EXPIRED, and a NEW uuid is still refused: the count saw them.
        past = t0 + _TTL_MS + 1
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(
                c, "u3", None, None, 1, "h3", past, 2, _TTL_MS)
        ) == queries.ENROLL_AT_CAPACITY
        assert await db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()[0]
        ) == 2
    finally:
        await db.close()


async def test_upsert_enroll_request_capped_enforces_capacity_atomically(tmp_path):
    # The capacity gate must be authoritative under one transaction: a NEW install_uuid is
    # rejected once the list is at max_pending, but an EXISTING one is always accepted (an
    # update, not a new row — so a full list can still refresh last_seen_at under TTL).
    # This is now the ONLY capacity gate: the channel's advisory pre-count was removed (it
    # was a second unauthenticated DB read per socket and could never be authoritative).
    db = await _make_db(tmp_path)
    try:
        # Fill to a max_pending of 2.
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(c, "u1", None, None, 1, "h1", 100, 2, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(c, "u2", None, None, 1, "h2", 100, 2, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED
        # A third, NEW uuid at capacity is refused and writes nothing.
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(c, "u3", None, None, 1, "h3", 100, 2, _TTL_MS)
        ) == queries.ENROLL_AT_CAPACITY
        assert await db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()[0]
        ) == 2
        assert await db.read(
            lambda c: c.execute("SELECT 1 FROM enroll_requests WHERE install_uuid='u3'").fetchone()
        ) is None
        # An EXISTING uuid at capacity is still accepted (refresh), count unchanged.
        assert await db.write(
            lambda c: queries.upsert_enroll_request_capped(c, "u1", None, "T1b", 1, "h1", 300, 2, _TTL_MS)
        ) == queries.ENROLL_ACCEPTED
        row = await db.read(
            lambda c: c.execute(
                "SELECT last_seen_at, suggested_title FROM enroll_requests "
                "WHERE install_uuid='u1'"
            ).fetchone()
        )
        assert row == (300, "T1b")
        assert await db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()[0]
        ) == 2
    finally:
        await db.close()
