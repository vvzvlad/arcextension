"""Revocation machinery (issue #35 §5/§6, Task D).

Unit-level coverage for the pieces the /admin handler (Task E) and the curator PASS
build on: the ``revoke_instance`` transaction (fields cleared + the MAIN 409 guard),
``read_revoked_relocations`` (what the pass retires), the ``known_instance_ids`` /
``load_preview_input`` ``status='active'`` filters, and the ``send_command`` safety net.
The end-to-end retire-pass + drain-cascade acceptance (8) lives in
``test_curator_runner.py`` where the extension emulator runs a real pass.
"""

from __future__ import annotations

import asyncio

import pytest

from src.db.access import Database
from src.db.actions import insert_action, read_revoked_relocations
from src.db.queries import (
    RevokeMainRefused,
    instance_status,
    revoke_instance,
)
from src.ext import protocol
from src.ext.commands import CommandError, send_command
from src.ext.registry import ConnState, Registry
from src.rules import access
from src.rules.preview import has_active_rules, load_preview_input, simulate


async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


def _seed_instance(conn, iid, *, status="active", session_id="s", connected=1,
                   snapshot_at=None):
    conn.execute(
        "INSERT INTO instances (id, status, session_id, connected, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (iid, status, session_id, connected, snapshot_at),
    )


async def _rows(db, sql, params=()):
    return await db.read(lambda c: c.execute(sql, params).fetchall())


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


async def _until(pred, timeout=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return pred()


# --- the transaction: fields cleared (acc 8 head) ---------------------------
async def test_revoke_instance_clears_session_stamps_status_and_revoked_at(tmp_path):
    """A non-main revoke sets status='revoked', revoked_at=now, and — the REQUIRED bit —
    session_id=NULL. Dropping the ``session_id = NULL`` clause reddens the session
    assertion (and the mirror would treat the instance's relocations as live forever)."""
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: _seed_instance(c, "gone", session_id="live-sess"))
        res = await db.write(
            lambda c: revoke_instance(c, "gone", now=5000, main_instance_id="main")
        )
        assert res.revoked is True and res.was_main is False and res.instance_id == "gone"
        row = await _rows(
            db, "SELECT status, session_id, revoked_at FROM instances WHERE id='gone'"
        )
        assert row == [("revoked", None, 5000)]
        # A revoke of an id with no row reports revoked=False (Task E → 404), no crash.
        res2 = await db.write(
            lambda c: revoke_instance(c, "ghost", now=6000, main_instance_id="main")
        )
        assert res2.revoked is False
    finally:
        await db.close()


async def test_revoke_clears_a_stuck_connected_flag_with_no_live_socket(tmp_path):
    """Revoking an OFFLINE instance must clear ``connected``, because nothing else will.

    The revoke handler's socket close is best-effort AND registry-driven: with no entry it
    returns immediately (``admin._close_live_socket``). ``mark_disconnected`` only ever runs
    from a socket's own finalizer and is epoch-guarded, so there is no other writer either.
    A row left ``connected=1`` by a process kill — the state a crash leaves behind — used
    to stay that way forever after a revoke, and a "connected" instance whose snapshot
    never advances is precisely what ``curator-instance-snapshot-stale`` reads as a
    half-open socket. Reddens if the ``connected = 0`` clause leaves ``_REVOKE_UPDATE``.
    """
    db = await _make_db(tmp_path)
    try:
        # connected=1 with NO registry entry anywhere: an instance whose process died.
        await db.write(lambda c: _seed_instance(c, "stuck", connected=1))
        await db.write(
            lambda c: c.execute("UPDATE instances SET focused_window_id = 7 WHERE id='stuck'")
        )
        await db.write(
            lambda c: revoke_instance(c, "stuck", now=9000, main_instance_id="main")
        )
        assert await _rows(
            db, "SELECT status, connected, focused_window_id FROM instances WHERE id='stuck'"
        ) == [("revoked", 0, None)]
    finally:
        await db.close()


# --- MAIN guard (acc 9) -----------------------------------------------------
async def test_revoke_main_requires_matching_replacement(tmp_path):
    """Revoking MAIN is refused (RevokeMainRefused → 409) unless replacement == the
    current MAIN_INSTANCE_ID; a matching replacement is allowed. Removing the guard
    reddens the two ``pytest.raises`` (the refusal would silently revoke MAIN)."""
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: _seed_instance(c, "main", session_id="m-sess"))

        # No replacement => refused, and NOTHING is written (still active with its session).
        with pytest.raises(RevokeMainRefused):
            await db.write(
                lambda c: revoke_instance(c, "main", now=1, main_instance_id="main")
            )
        # A WRONG replacement => refused.
        with pytest.raises(RevokeMainRefused):
            await db.write(
                lambda c: revoke_instance(
                    c, "main", now=1, main_instance_id="main", replacement="prox"
                )
            )
        assert await _rows(
            db, "SELECT status, session_id FROM instances WHERE id='main'"
        ) == [("active", "m-sess")]

        # The matching replacement (MAIN replaced in place) is allowed.
        res = await db.write(
            lambda c: revoke_instance(
                c, "main", now=7, main_instance_id="main", replacement="main"
            )
        )
        assert res.revoked is True and res.was_main is True
        assert await _rows(
            db, "SELECT status, session_id, revoked_at FROM instances WHERE id='main'"
        ) == [("revoked", None, 7)]
    finally:
        await db.close()


# --- known_instance_ids is ACTIVE-only (§6) ---------------------------------
async def test_known_instance_ids_excludes_revoked_and_pending(tmp_path):
    """Only ``status='active'`` rows are "known". Reverting the filter (SELECT id FROM
    instances) reddens: the revoked/pending ids would leak back in."""
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: (
            _seed_instance(c, "live", status="active"),
            _seed_instance(c, "gone", status="revoked"),
            _seed_instance(c, "wait", status="pending"),
        ))
        known = await db.read(access.known_instance_ids)
        assert known == {"live"}
    finally:
        await db.close()


# --- read_revoked_relocations: what the pass retires ------------------------
async def test_read_revoked_relocations_matches_source_or_target_excludes_pending(tmp_path):
    """The retire scan returns live relocate rows touching a revoked instance on EITHER
    side, and EXCLUDES a relocate that already has a pending/done relocate_close (owned by
    reconcile / already retired). Reddens on a source-only match, or if it forgets the
    pending-close exclusion."""
    db = await _make_db(tmp_path)
    try:
        await db.write(lambda c: (
            _seed_instance(c, "gone", status="revoked"),
            _seed_instance(c, "live", status="active"),
        ))

        async def _reloc(**kw):
            kw.setdefault("ts", 1)
            kw.setdefault("kind", "relocate")
            kw.setdefault("status", "done")
            kw.setdefault("initiator", "curator")
            return await db.write(lambda c: insert_action(c, **kw))

        # (1) source revoked => retired.
        src_id = await _reloc(instance_from="gone", instance_to="live",
                              url="https://a/1", url_norm="https://a/1")
        # (2) target revoked => retired.
        dst_id = await _reloc(instance_from="live", instance_to="gone",
                              url="https://a/2", url_norm="https://a/2")
        # (3) neither endpoint revoked => untouched.
        await _reloc(instance_from="live", instance_to="live",
                     url="https://a/3", url_norm="https://a/3")
        # (4) source revoked BUT a pending relocate_close exists => excluded (reconcile owns it).
        pend_reloc = await _reloc(instance_from="gone", instance_to="live",
                                  url="https://a/4", url_norm="https://a/4")
        await db.write(lambda c: insert_action(
            c, ts=2, kind="relocate_close", status="pending", initiator="curator",
            origin_action_id=pend_reloc, instance_from="gone", instance_to="live",
            url="https://a/4", url_norm="https://a/4"))

        found = set(await db.read(read_revoked_relocations))
        assert found == {src_id, dst_id}
    finally:
        await db.close()


# --- load_preview_input filter + the main-exemption DECISION -----------------
async def test_load_preview_input_excludes_revoked_tabs_but_keeps_the_drain(tmp_path):
    """A revoked instance's tabs must not contribute to the preview, even in the window
    where its socket close has not yet flipped ``connected`` to 0 — and the filter must
    NOT switch the drain off (it is a property of the RULE set, not the instance list).

    A candidate rule ``keep.lc -> live`` is valid, so ``enables_drain`` is True. The
    revoked ``gone`` instance holds a tab that WOULD relocate to ``live`` if it were
    counted. With the ``status='active'`` filter it is excluded => zero relocations.
    Reverting the filter reddens (gone is counted, its tab relocates: relocations==1)."""
    db = await _make_db(tmp_path)
    try:
        now = 1_000_000
        await db.write(lambda c: (
            _seed_instance(c, "live", status="active", connected=1, snapshot_at=now),
            # Revoked but still connected=1 & freshly snapshotted (the transient race).
            _seed_instance(c, "gone", status="revoked", connected=1, snapshot_at=now),
        ))
        # gone holds an idle tab matching keep.lc -> live (would relocate if counted).
        await db.write(lambda c: c.execute(
            "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, pinned, active, "
            "opened_at, last_active_at, age_unknown, self_navigating, audible, updated_at) "
            "VALUES ('gone', 5, 1, 'https://keep.lc/1', 't', 0, 0, 0, ?, 0, 0, 0, ?)",
            (now - 10 * 3_600_000, now)))
        await db.write(lambda c: c.execute(
            "INSERT INTO windows (instance_id, window_id, type, state) "
            "VALUES ('gone', 1, 'normal', 'normal')"))

        candidate = [{"id": 1, "pattern": "keep.lc", "instance_id": "live",
                      "singleton": 0, "invalid": 0}]
        inp = await db.read(lambda c: load_preview_input(
            c, candidate, now=now, idle_ms=60 * 60_000,
            state_fresh_ms=10 * 60_000, main_instance_id="main"))
        res = simulate(inp)

        assert res.enables_drain is True          # the drain is NOT disabled by the filter
        assert res.relocations == 0               # the revoked instance contributed nothing
        # And ``gone`` is not even a countable instance in the freshness table.
        assert all(i["id"] != "gone" or not i["counted"] for i in res.instances)
    finally:
        await db.close()


# --- send_command safety net (§5 / Task D §4) -------------------------------
async def test_send_command_to_revoked_instance_fails_like_no_connection(tmp_path):
    """A command to a revoked instance (whose live socket lost the close race) fails with
    ERR_NO_CONNECTION and puts NO frame on the wire. The control arm — flipping the row
    back to 'active' and seeing the frame sent — proves the refusal is status-driven, not
    a blanket block (non-vacuous)."""
    db = await _make_db(tmp_path)
    try:
        reg = Registry()
        cs = ConnState(ws=_FakeWS(), conn_epoch=1, install_uuid="u", session_id="s")
        reg.put("gone", cs)
        await db.write(lambda c: _seed_instance(c, "gone", status="revoked"))

        with pytest.raises(CommandError) as ei:
            await send_command(reg, db, "gone", protocol.CMD_GET_TAB, {"tabId": 1},
                               cmd_timeout_ms=1000)
        assert ei.value.code == protocol.ERR_NO_CONNECTION
        assert cs.ws.sent == []          # nothing reached the socket

        # Control: an ACTIVE row IS dispatched (the guard is specific to non-active).
        await db.write(lambda c: c.execute(
            "UPDATE instances SET status='active' WHERE id='gone'"))
        task = asyncio.create_task(
            send_command(reg, db, "gone", protocol.CMD_GET_TAB, {"tabId": 1},
                         cmd_timeout_ms=1000))
        assert await _until(lambda: cs.ws.sent), "an active target must be dispatched"
        # Resolve so the task ends cleanly.
        from src.ext.commands import resolve_response
        resolve_response(cs, {"type": protocol.TYPE_RESPONSE,
                              "id": cs.ws.sent[-1]["id"], "ok": True, "result": {}})
        await task

        # Sanity: instance_status reads the raw status.
        assert await db.read(lambda c: instance_status(c, "gone")) == "active"
    finally:
        await db.close()
