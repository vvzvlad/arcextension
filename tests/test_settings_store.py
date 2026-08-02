"""The runtime ``settings`` table helpers, incl. the execute_js kill-switch (§12)."""

import sqlite3

import pytest

from src.db.access import Database
from src.db.settings_store import (
    EXECUTE_JS_ENABLED_KEY,
    get_setting,
    is_execute_js_enabled,
    set_execute_js_enabled,
    set_setting,
)


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


async def test_execute_js_default_enabled_when_row_absent(tmp_path):
    # A fresh DB has never flipped the switch => execute_js is ENABLED by default.
    db = await _make_db(tmp_path)
    try:
        assert await db.read(is_execute_js_enabled) is True
    finally:
        await db.close()


async def test_execute_js_toggle_off_then_on(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: set_execute_js_enabled(c, False))
        assert await db.read(is_execute_js_enabled) is False
        await db.write(lambda c: set_execute_js_enabled(c, True))
        assert await db.read(is_execute_js_enabled) is True
    finally:
        await db.close()


@pytest.mark.parametrize("value,expected", [
    ("0", False),
    ("false", False),
    ("off", False),
    ("no", False),
    ("FALSE", False),
    ("1", True),
    ("true", True),
    ("", True),          # unrecognised => fail OPEN (default enabled)
    ("garbage", True),   # unrecognised => fail OPEN
])
async def test_execute_js_enabled_value_interpretation(tmp_path, value, expected):
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: set_setting(c, EXECUTE_JS_ENABLED_KEY, value))
        assert await db.read(is_execute_js_enabled) is expected
    finally:
        await db.close()
