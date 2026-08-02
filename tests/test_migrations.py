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


# --- Version 2: enrollment schema (§13) -------------------------------------
def test_migrate_creates_enrollment_schema(tmp_path):
    # After a full migrate the enrollment step-2 objects exist and the version is 2.
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        result = migrate(conn, db, backups)
        assert result.ok and not result.degraded
        # PRAGMA user_version tracks MAX_VERSION, which the enrollment step bumped to 2.
        assert _version(conn) == MAX_VERSION == 2

        tables = _tables(conn)
        assert {"enroll_requests", "admin_audit"} <= tables

        # The new instances columns.
        assert {
            "status", "secret_hash", "install_uuid", "enrolled_at", "revoked_at"
        } <= _columns(conn, "instances")

        # The UNIQUE index guards secret_hash (unique flag == 1).
        assert _index_list(conn, "instances").get("instances_secret_hash") == 1
        # The admin_audit(ts) lookup index exists.
        assert "admin_audit_ts" in _index_list(conn, "admin_audit")
    finally:
        conn.close()


def test_pre_migration_instance_becomes_revoked(tmp_path):
    # A row that existed before enrollment (secret_hash IS NULL) is migrated to
    # 'revoked' — after the upgrade even MAIN needs explicit re-approval.
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        _migrate_to_v1_only(conn, db, backups)
        conn.execute("INSERT INTO instances (id, title) VALUES ('main', 'Main')")

        result = migrate(conn, db, backups)  # applies step 2
        assert result.ok and _version(conn) == MAX_VERSION

        row = conn.execute(
            "SELECT status, secret_hash FROM instances WHERE id = 'main'"
        ).fetchone()
        assert row[0] == "revoked"
        assert row[1] is None
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


def test_v1_to_v2_is_idempotent_on_rerun(tmp_path):
    # Rolling redeploy: a DB brought from v1 to v2 must re-run migrate as a no-op.
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        _migrate_to_v1_only(conn, db, backups)
        r2 = migrate(conn, db, backups)  # v1 -> v2
        assert r2.ok and _version(conn) == MAX_VERSION
        r3 = migrate(conn, db, backups)  # v2 -> v2, pure no-op
        assert r3.ok and not r3.degraded and _version(conn) == MAX_VERSION
        assert {"enroll_requests", "admin_audit"} <= _tables(conn)
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
