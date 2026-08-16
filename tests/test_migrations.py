import sqlite3
import threading

from pathlib import Path

import pytest

from src.db.migrations import migrate
from src.db.schema import MAX_VERSION, STEPS


def _count_backups(backups_dir):
    p = Path(backups_dir)
    return len(list(p.glob("curator-*.db"))) if p.exists() else 0


def _open(path):
    # isolation_level=None so our explicit BEGIN/COMMIT/ROLLBACK are honored and
    # stdlib does not inject an implicit transaction.
    return sqlite3.connect(path, check_same_thread=False, isolation_level=None)


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {r[0] for r in rows}


def _version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_list(conn, table):
    # PRAGMA index_list rows: (seq, name, unique, origin, partial).
    return {r[1]: r[2] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def _migrate_to_v1_only(conn, db, backups):
    """Bring a fresh DB to version 1 only (the pre-enrollment schema)."""
    r = migrate(conn, db, backups, steps=[STEPS[0]], max_version=1)
    assert r.ok and _version(conn) == 1
    return r


EXPECTED_TABLES = {
    "instances", "tabs", "windows", "rules", "actions", "passes",
    "js_audit", "quarantine", "exemptions", "quick_links", "settings",
}


def test_migrate_from_empty_then_idempotent(tmp_path):
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")

    conn = _open(db)
    try:
        result = migrate(conn, db, backups)
        assert result.ok and not result.degraded
        assert result.final_version == MAX_VERSION
        assert EXPECTED_TABLES <= _tables(conn)

        # Second run is a pure no-op: version unchanged, no error, tables intact.
        result2 = migrate(conn, db, backups)
        assert result2.ok and not result2.degraded
        assert result2.final_version == MAX_VERSION
        assert EXPECTED_TABLES <= _tables(conn)
    finally:
        conn.close()


def test_concurrent_runners_no_duplicate_column(tmp_path):
    # The rolling-redeploy scenario: two runners hit the same file at once and
    # neither may crash on "duplicate column".
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    # Materialize the file so both connections open the same DB.
    sqlite3.connect(db).close()

    results = {}
    barrier = threading.Barrier(2)

    def run(name):
        conn = _open(db)
        try:
            barrier.wait()
            results[name] = migrate(conn, db, backups)
        finally:
            conn.close()

    t1 = threading.Thread(target=run, args=("a",))
    t2 = threading.Thread(target=run, args=("b",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert set(results) == {"a", "b"}
    for name, r in results.items():
        assert r.ok, f"{name} not ok: {r.reason}"
        assert not r.degraded, f"{name} degraded: {r.reason}"
        assert r.final_version == MAX_VERSION

    conn = _open(db)
    try:
        assert _version(conn) == MAX_VERSION
        assert EXPECTED_TABLES <= _tables(conn)
    finally:
        conn.close()


def test_no_safety_copy_when_nothing_to_migrate(tmp_path):
    # A fresh DB (version 0) has no schema to protect, and an already-migrated DB
    # (version == MAX) applies no step — neither must run a per-boot VACUUM INTO,
    # which would delay /healthz behind Traefik and evict nightly copies.
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        migrate(conn, db, backups)  # 0 -> MAX: no copy (version-0, nothing to protect)
        assert _count_backups(backups) == 0
        # Restart a stable, already-migrated DB several times: still no copies.
        migrate(conn, db, backups)
        migrate(conn, db, backups)
        assert _count_backups(backups) == 0
    finally:
        conn.close()


def test_safety_copy_taken_when_a_step_is_pending(tmp_path):
    # An existing schema with a pending step MUST get a pre-migration safety copy.
    # De-hardcoded off MAX_VERSION: the synthetic pending step targets MAX_VERSION+1 so
    # this test keeps meaning as real steps are appended (it used to hardcode `2`, which
    # now collides with the shipped enrollment step).
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    next_version = MAX_VERSION + 1
    conn = _open(db)
    try:
        migrate(conn, db, backups)  # bring to MAX_VERSION, no copy yet
        assert _count_backups(backups) == 0
        extended = list(STEPS) + [(next_version, ["CREATE TABLE extra (x INTEGER)"])]
        result = migrate(conn, db, backups, steps=extended, max_version=next_version)
        assert result.ok and result.final_version == next_version
        assert _count_backups(backups) == 1  # copy taken before applying the pending step
    finally:
        conn.close()


def test_version_ahead_is_degraded(tmp_path):
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")

    seed = _open(db)
    seed.execute(f"PRAGMA user_version = {MAX_VERSION + 1}")
    seed.close()

    conn = _open(db)
    try:
        result = migrate(conn, db, backups)
        assert result.degraded and not result.ok
        assert result.final_version == MAX_VERSION + 1
        # Schema untouched — no tables were created.
        assert not (EXPECTED_TABLES & _tables(conn))
    finally:
        conn.close()


# --- Version 2/3: the enrollment schema as it stands today ------------------
def test_migrate_creates_enrollment_schema(tmp_path):
    # After a full migrate the surviving enrollment objects exist and the version tracks
    # MAX_VERSION.
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        result = migrate(conn, db, backups)
        assert result.ok and not result.degraded
        # PRAGMA user_version tracks MAX_VERSION. NOT compared against a literal: the
        # enrollment step's version is an implementation detail that moves the moment a
        # step is appended, and a hardcoded number here would redden a change that is
        # correct — the very hardcoding issue #35 asked to remove (it survived one round in
        # this same assertion). What this test is ABOUT is the objects below.
        assert _version(conn) == MAX_VERSION

        tables = _tables(conn)
        assert "admin_audit" in tables
        # Step 3 dropped the pending-request storage: enrolment is one step (§6), so there
        # is no list to hold. Asserted rather than merely not-mentioned, because a table
        # left standing is an invitation to wire the second step back in.
        assert "enroll_requests" not in tables

        # The instances columns step 2 added...
        assert {
            "status", "secret_hash", "install_uuid", "enrolled_at", "revoked_at"
        } <= _columns(conn, "instances")
        # ...and the one step 3 removed: the id IS the name now (§6).
        assert "title" not in _columns(conn, "instances")

        # The UNIQUE index guards secret_hash (unique flag == 1). It must SURVIVE the
        # column drop — SQLite rebuilds the table under ALTER TABLE ... DROP COLUMN, and an
        # index lost there would silently allow two instances to share one credential.
        assert _index_list(conn, "instances").get("instances_secret_hash") == 1
        # The admin_audit(ts) lookup index exists.
        assert "admin_audit_ts" in _index_list(conn, "admin_audit")
    finally:
        conn.close()


def test_pre_migration_instance_becomes_revoked(tmp_path):
    # A row that existed before enrollment (secret_hash IS NULL) is migrated to
    # 'revoked' — after the upgrade even MAIN needs an explicit re-enrolment.
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        _migrate_to_v1_only(conn, db, backups)
        conn.execute("INSERT INTO instances (id, title) VALUES ('main', 'Main')")

        result = migrate(conn, db, backups)  # applies steps 2 and 3
        assert result.ok and _version(conn) == MAX_VERSION

        row = conn.execute(
            "SELECT status, secret_hash FROM instances WHERE id = 'main'"
        ).fetchone()
        assert row[0] == "revoked"
        assert row[1] is None
    finally:
        conn.close()


def test_step3_keeps_existing_instances_and_drops_only_title(tmp_path):
    """The upgrade on a NON-EMPTY database: rows survive, ``title`` goes, the table goes.

    ``ALTER TABLE ... DROP COLUMN`` rebuilds the table, which is exactly when data is
    quietly lost — so this seeds a v2 database that looks like a live install (two
    instances, one of them enrolled with a secret, plus pending enroll_requests) and pins
    what must be true afterwards. Reddens if step 3 is implemented as a recreate that
    forgets to copy rows across.
    """
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        # Bring the DB to version 2 only (the pre-simplification schema).
        migrate(conn, db, backups, steps=STEPS[:2], max_version=2)
        assert _version(conn) == 2
        conn.execute(
            "INSERT INTO instances (id, title, status, secret_hash, install_uuid, "
            "conn_epoch, allow_execute_js) VALUES "
            "('main', 'Curator Main', 'active', 'hash-main', 'uuid-main', 7, 1)"
        )
        conn.execute(
            "INSERT INTO instances (id, title, status) VALUES ('prox', 'Prox', 'revoked')"
        )
        conn.execute(
            "INSERT INTO enroll_requests (install_uuid, origin, suggested_title, "
            "protocol_version, secret_hash, first_seen_at, last_seen_at) "
            "VALUES ('uuid-waiting', 'chrome-extension://x', 'Waiting', 1, 'h', 1, 1)"
        )
        conn.commit()

        result = migrate(conn, db, backups)  # applies step 3
        assert result.ok and not result.degraded
        assert _version(conn) == MAX_VERSION

        # Every instance survived, with every OTHER column intact.
        rows = conn.execute(
            "SELECT id, status, secret_hash, install_uuid, conn_epoch, allow_execute_js "
            "FROM instances ORDER BY id"
        ).fetchall()
        assert rows == [
            ("main", "active", "hash-main", "uuid-main", 7, 1),
            ("prox", "revoked", None, None, 0, 0),
        ]
        assert "title" not in _columns(conn, "instances")
        assert "enroll_requests" not in _tables(conn)
        # The credential guard survived the table rebuild.
        assert _index_list(conn, "instances").get("instances_secret_hash") == 1
    finally:
        conn.close()


def test_multiple_null_secret_hash_rows_coexist_but_dupes_collide(tmp_path):
    # NULL under a UNIQUE index does not collide in SQLite, so many not-yet-enrolled
    # rows coexist; two identical NON-null hashes still collide (the guard works).
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        migrate(conn, db, backups)
        for iid in ("a", "b", "c"):
            conn.execute("INSERT INTO instances (id) VALUES (?)", (iid,))
        n_null = conn.execute(
            "SELECT COUNT(*) FROM instances WHERE secret_hash IS NULL"
        ).fetchone()[0]
        assert n_null == 3  # three NULLs coexist under the UNIQUE index

        # A row inserted AFTER migration (no explicit status) defaults to 'pending',
        # NOT 'active' — the load-bearing invariant that keeps a post-migration hello
        # from silently re-activating an unapproved instance. If the DEFAULT were ever
        # changed to 'active', this reddens.
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM instances WHERE status = 'pending'"
        ).fetchone()[0]
        assert n_pending == 3

        # Two rows with the SAME non-null secret_hash must be rejected.
        conn.execute("UPDATE instances SET secret_hash = 'h' WHERE id = 'a'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE instances SET secret_hash = 'h' WHERE id = 'b'")
    finally:
        conn.close()


def test_v1_to_latest_is_idempotent_on_rerun(tmp_path):
    # Rolling redeploy: a DB brought from v1 to the head must re-run migrate as a no-op.
    # This is where a NON-idempotent step 3 would show up as a crash-loop: `DROP TABLE`
    # and `DROP COLUMN` both fail hard on a second application, and the version guard
    # inside the transaction is the only thing that stops them running twice.
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        _migrate_to_v1_only(conn, db, backups)
        r2 = migrate(conn, db, backups)  # v1 -> head
        assert r2.ok and _version(conn) == MAX_VERSION
        r3 = migrate(conn, db, backups)  # head -> head, pure no-op
        assert r3.ok and not r3.degraded and _version(conn) == MAX_VERSION
        assert "admin_audit" in _tables(conn)
    finally:
        conn.close()


def test_step4_adds_the_capability_columns_with_honest_defaults(tmp_path):
    """The §11 capability report's storage, on a database that already has live rows.

    Both defaults are load-bearing. ``allow_debugger`` DEFAULT 0: a column defaulting to 1
    would report every pre-migration instance as debugger-capable — the opposite of the
    truth, and exactly the kind of false capability an agent would then act on.
    ``ext_version`` NULL: an instance that has not said hello since the upgrade genuinely
    has not told us which bundle it runs, and "unknown" must not be spelled as a made-up
    version string.
    """
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        # A live-looking v3 database (one enrolled instance that opted INTO execute_js).
        migrate(conn, db, backups, steps=STEPS[:3], max_version=3)
        assert _version(conn) == 3
        assert "allow_debugger" not in _columns(conn, "instances")
        conn.execute(
            "INSERT INTO instances (id, status, secret_hash, conn_epoch, allow_execute_js) "
            "VALUES ('main', 'active', 'hash-main', 7, 1)"
        )
        conn.commit()

        result = migrate(conn, db, backups)  # applies step 4
        assert result.ok and not result.degraded
        assert _version(conn) == MAX_VERSION
        assert {"allow_debugger", "ext_version"} <= _columns(conn, "instances")

        row = conn.execute(
            "SELECT allow_execute_js, allow_debugger, ext_version FROM instances "
            "WHERE id = 'main'"
        ).fetchone()
        # The pre-existing opt-in survived; the two new facts read as "not told us".
        assert row == (1, 0, None)
    finally:
        conn.close()


def test_step5_records_how_audited_code_ran_and_backfills_the_only_honest_answer(tmp_path):
    """``js_audit.await_promise``, DEFAULT 0 on rows written before the column existed.

    0 is not a convenience default: every pre-migration row came from the eval path,
    because that was the only path there was. A default of 1 would retroactively claim
    async-function semantics for code that never had them.
    """
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        migrate(conn, db, backups, steps=STEPS[:4], max_version=4)
        assert "await_promise" not in _columns(conn, "js_audit")
        conn.execute(
            "INSERT INTO js_audit (ts, instance_id, code, initiator) "
            "VALUES (1, 'main', 'document.title', 'mcp')"
        )
        conn.commit()

        result = migrate(conn, db, backups)  # applies step 5
        assert result.ok and not result.degraded
        assert _version(conn) == MAX_VERSION
        assert "await_promise" in _columns(conn, "js_audit")
        assert conn.execute(
            "SELECT code, await_promise FROM js_audit"
        ).fetchone() == ("document.title", 0)
    finally:
        conn.close()


def test_failing_step_is_degraded_and_rolls_back(tmp_path):
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")

    bad_steps = [(1, ["CREATE TABLE ok_table (x INTEGER)", "THIS IS NOT SQL"])]

    conn = _open(db)
    try:
        result = migrate(conn, db, backups, steps=bad_steps, max_version=1)
        assert result.degraded and not result.ok
        # BEGIN IMMEDIATE means the partial DDL AND the version bump rolled back.
        assert _version(conn) == 0
        assert "ok_table" not in _tables(conn)
    finally:
        conn.close()
