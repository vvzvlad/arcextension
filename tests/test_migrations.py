import sqlite3
import threading

from pathlib import Path

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
    db = str(tmp_path / "curator.db")
    backups = str(tmp_path / "backups")
    conn = _open(db)
    try:
        migrate(conn, db, backups)  # bring to version 1 (== MAX), no copy yet
        assert _count_backups(backups) == 0
        two_steps = list(STEPS) + [(2, ["CREATE TABLE extra (x INTEGER)"])]
        result = migrate(conn, db, backups, steps=two_steps, max_version=2)
        assert result.ok and result.final_version == 2
        assert _count_backups(backups) == 1  # copy taken before applying step 2
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
