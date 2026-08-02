"""actions writes (§4): normalize_url + insert_action decision-basis round-trip."""

import sqlite3

import pytest

from src.db.access import Database
from src.db.actions import insert_action, normalize_url


def test_normalize_url_strips_query_and_fragment():
    assert normalize_url("https://a.com/p/q?x=1&y=2#frag") == "https://a.com/p/q"
    assert normalize_url("https://a.com:8443/p?z") == "https://a.com:8443/p"
    assert normalize_url("https://a.com") == "https://a.com"
    assert normalize_url(None) is None
    assert normalize_url("") is None


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def test_insert_action_preserves_decision_basis(tmp_path):
    db = await _make_db(tmp_path)
    try:
        aid = await db.write(
            lambda c: insert_action(
                c,
                pass_id="p-1",
                ts=1_700_000_000_000,
                kind="relocate",
                status="done",
                initiator="curator",
                instance_from="src",
                instance_to="dst",
                tab_id=5,
                session_id_from="sess-A",
                tab_id_to=9,
                session_id_to="sess-B",
                origin_action_id=None,
                rule_id=3,
                rule_pattern="*.example.com/*",
                decision="rule_home",
                src_opened_at=111,
                src_last_active_at=222,
                src_age_unknown=1,
                url="https://x.com/y?tracking=1",
                url_norm="https://x.com/y",
                title="Dashboard",
                pinned=1,
                reason=None,
                detail="survivor:7",
            )
        )

        def _read(c):
            c.row_factory = sqlite3.Row
            return c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()

        row = await db.read(_read)
        # The POINT of §4: decision basis survives even after the rule is deleted.
        assert row["rule_id"] == 3
        assert row["rule_pattern"] == "*.example.com/*"
        assert row["decision"] == "rule_home"
        # Sessions survive the row (needed once the archive outlives the session).
        assert row["session_id_from"] == "sess-A"
        assert row["session_id_to"] == "sess-B"
        # Full round-trip of the rest.
        assert row["pass_id"] == "p-1"
        assert row["kind"] == "relocate"
        assert row["status"] == "done"
        assert row["initiator"] == "curator"
        assert row["instance_from"] == "src"
        assert row["instance_to"] == "dst"
        assert row["tab_id"] == 5
        assert row["tab_id_to"] == 9
        assert row["src_opened_at"] == 111
        assert row["src_last_active_at"] == 222
        assert row["src_age_unknown"] == 1
        assert row["url"] == "https://x.com/y?tracking=1"
        assert row["url_norm"] == "https://x.com/y"
        assert row["title"] == "Dashboard"
        assert row["pinned"] == 1
        assert row["detail"] == "survivor:7"
        assert row["restored_at"] is None
    finally:
        await db.close()


async def test_insert_action_validates_enums(tmp_path):
    db = await _make_db(tmp_path)
    try:
        for bad in (
            dict(kind="teleport", status="done", initiator="curator"),
            dict(kind="restore", status="pending", initiator="curator"),
            dict(kind="restore", status="done", initiator="root"),
        ):
            with pytest.raises(ValueError):
                await db.write(lambda c, b=bad: insert_action(c, ts=1, **b))
        # 'abandoned' is a legit terminal status introduced in Фаза 4.
        aid = await db.write(
            lambda c: insert_action(
                c, ts=1, kind="relocate", status="abandoned", initiator="curator"
            )
        )
        assert isinstance(aid, int)
    finally:
        await db.close()
