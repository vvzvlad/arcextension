"""The runtime ``settings`` table generic helpers (get_setting / set_setting)."""

from src.db.access import Database
from src.db.settings_store import get_setting, set_setting


async def _make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def test_get_set_setting_round_trip(tmp_path):
    db = await _make_db(tmp_path)
    try:
        assert await db.read(lambda c: get_setting(c, "missing")) is None
        assert await db.read(lambda c: get_setting(c, "missing", "fallback")) == "fallback"
        await db.write(lambda c: set_setting(c, "k", "v1"))
        assert await db.read(lambda c: get_setting(c, "k")) == "v1"
        # Upsert overwrites the whole value.
        await db.write(lambda c: set_setting(c, "k", "v2"))
        assert await db.read(lambda c: get_setting(c, "k")) == "v2"
    finally:
        await db.close()
