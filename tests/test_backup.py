import sqlite3
import time

import pytest

from src.db import backup
from src.db.backup import run_backup


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1), (2), (3)")
    conn.commit()
    conn.close()


def test_run_backup_valid_and_no_leftover_tmp(tmp_path):
    db = tmp_path / "curator.db"
    _seed_db(db)
    backups = tmp_path / "backups"

    out = run_backup(str(db), str(backups))

    assert out.exists()
    assert out.suffix == ".db"
    # No temp file left behind.
    assert list(backups.glob("*.tmp")) == []
    # The copy passes its own integrity check and carries the data.
    conn = sqlite3.connect(out)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    finally:
        conn.close()


def test_backup_failure_after_tmp_leaves_no_tmp(tmp_path, monkeypatch):
    # The full-disk guarantee (§12): a failure after the .tmp is written must not
    # leave a DB-sized turd behind. Force a post-write failure via the verify step.
    db = tmp_path / "curator.db"
    _seed_db(db)
    backups = tmp_path / "backups"

    def boom(copy_path):
        raise RuntimeError("simulated integrity failure")

    monkeypatch.setattr(backup, "_verify_backup", boom)

    with pytest.raises(RuntimeError):
        run_backup(str(db), str(backups))

    # No temp file and no promoted copy remain.
    assert list(backups.glob("*.tmp")) == []
    assert list(backups.glob("curator-*.db")) == []


def test_rotation_keeps_last_n(tmp_path):
    db = tmp_path / "curator.db"
    _seed_db(db)
    backups = tmp_path / "backups"

    for _ in range(6):
        run_backup(str(db), str(backups), keep=3)
        time.sleep(0.01)  # distinct microsecond timestamps

    copies = sorted(backups.glob("curator-*.db"))
    assert len(copies) == 3  # rotation deleted the 3 oldest
