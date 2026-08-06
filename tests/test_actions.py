"""actions writes (§4): normalize_url + insert_action decision-basis round-trip."""

import sqlite3

import pytest

from src.db.access import Database
from src.db.actions import insert_action, normalize_url, read_pending_closes


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
            dict(kind="restore", status="teleporting", initiator="curator"),
            dict(kind="restore", status="done", initiator="root"),
        ):
            with pytest.raises(ValueError):
                await db.write(lambda c, b=bad: insert_action(c, ts=1, **b))
        # 'abandoned' is a legit terminal status introduced in Фаза 4; 'pending' is
        # the in-flight close status introduced in Фаза 16 (WARNING-1).
        for good in ("abandoned", "pending"):
            aid = await db.write(
                lambda c, s=good: insert_action(
                    c, ts=1, kind="relocate_close", status=s, initiator="curator"
                )
            )
            assert isinstance(aid, int)
    finally:
        await db.close()


async def test_read_pending_closes_grace_excludes_in_flight_rows(tmp_path):
    # Acceptance 10 (#48): the synchronous relocate_tab verb writes a `pending`
    # relocate_close BEFORE its open_tab/get_tab/close round-trips, so a pass firing INSIDE
    # that command window must not reconcile the row (a second close / a phantom abandon).
    # The `ts < now - 3*cmd_timeout_ms` grace enforces it: the row's ts is stamped before
    # open (≤1×), get_tab (≤1×) and the source close (≤1×), so a sender may still be in
    # flight up to 3× a command timeout; only a row older than that is reconcilable.
    db = await _make_db(tmp_path)
    try:
        now = 10_000_000
        cmd_timeout_ms = 20_000  # 3x = 60_000
        # IN-FLIGHT: 2x old — the source close may still be unsent (open+get_tab pending).
        fresh = await db.write(lambda c: insert_action(
            c, ts=now - 40_000, kind="relocate_close", status="pending", initiator="mcp",
            instance_from="themed", tab_id=5, url="https://a/b", url_norm="https://a/b"))
        # AGED OUT: older than the full 3x budget — no sender can still be in flight.
        stale = await db.write(lambda c: insert_action(
            c, ts=now - 70_000, kind="relocate_close", status="pending", initiator="mcp",
            instance_from="themed", tab_id=6, url="https://a/c", url_norm="https://a/c"))
        rows = await db.read(lambda c: read_pending_closes(
            c, now=now, cmd_timeout_ms=cmd_timeout_ms))
        ids = {r["id"] for r in rows}
        assert stale in ids and fresh not in ids  # only the aged-out row is reconcilable
    finally:
        await db.close()
