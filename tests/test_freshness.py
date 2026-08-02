"""The SHARED out-of-pass freshness mechanism (:mod:`src.api.freshness`, §6).

The load-bearing property under test is a NEGATIVE one: an out-of-pass caller (restore,
preview, reset) must never overwrite a ``pending_snapshot_id`` that somebody else — a
curator pass above all — is waiting on. The channel matches snapshot ids EXACTLY, so an
overwrite makes the instance's answer to the PASS be dropped and silently ejects it from
that pass. Each copy of the poll loop used to do exactly that.

The second property is the counterweight: a request that outlived its own timeout has
been abandoned by its sender, so the slot MUST be reclaimable — otherwise one extension
that never answers one request wedges every later restore/preview/reset.
"""

import asyncio
import time
from conftest import make_settings
from types import SimpleNamespace

from src.api.freshness import DISCONNECTED, FRESH, TIMEOUT, _now_ms, ensure_fresh
from src.db.access import Database
from src.ext.registry import ConnState, Registry


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


def _settings(**over):
    """This file's settings, from the shared surface in ``tests/conftest.py``.

    No ``tmp_path``: nothing here builds an app — these tests call the functions
    directly and open their own ``Database`` — so the factory leaves ``db_path`` /
    ``backup_dir`` off entirely rather than inventing one.
    """
    return make_settings(**{**{"snapshot_timeout_ms": 400}, **over})
async def _make_db(tmp_path):
    db = Database(str(tmp_path / "curator.db"), str(tmp_path / "backups"))
    await db.open()
    assert not db.degraded
    return db


async def _insert_instance(db, iid, *, session_id, snapshot_at, connected=1):
    await db.write(lambda c: c.execute(
        "INSERT INTO instances (id, connected, session_id, snapshot_at) VALUES (?,?,?,?)",
        (iid, connected, session_id, snapshot_at),
    ))


async def _set_snapshot_at(db, iid, value):
    await db.write(lambda c: c.execute(
        "UPDATE instances SET snapshot_at = ? WHERE id = ?", (value, iid)
    ))


def _connect(reg, iid="i1", session="s1"):
    ws = FakeWS()
    cs = ConnState(ws=ws, conn_epoch=1, install_uuid="u", session_id=session)
    reg.put(iid, cs)
    return cs, ws


# --- THE guard: a live foreign request is waited on, never clobbered --------
async def test_waits_for_a_live_pass_request_and_never_clobbers_it(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=0)  # stale mirror
    reg = Registry()
    cs, ws = _connect(reg)
    # A curator pass has its own request in flight, sent just now.
    cs.pending_snapshot_id = "pass-abc"
    cs.pending_sent_at = _now_ms()

    task = asyncio.create_task(ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=2000)))
    await asyncio.sleep(0.15)
    # Mid-wait: the pass's id is untouched and NO competing request went out. Remove
    # the guard and this is a `req-<uuid>` with a frame on the socket — the instance is
    # ejected from the pass.
    assert cs.pending_snapshot_id == "pass-abc"
    assert ws.sent == []

    # The pass's own snapshot lands (its id answered => the instance stays IN the pass)
    # and the refreshed mirror is what our caller wanted all along.
    await _set_snapshot_at(db, "i1", _now_ms())
    fresh, reason, out = await task
    assert (fresh, reason) == (True, FRESH) and out is cs
    assert ws.sent == []  # never sent one of our own


async def test_reclaims_a_request_that_outlived_its_own_timeout(tmp_path):
    # Counterweight: an id older than SNAPSHOT_TIMEOUT_MS has been given up on by its
    # sender (a pass waits exactly that long in runner._await_ready), so claiming it
    # ejects nobody. Without this, one unanswered request wedges the instance for every
    # later restore / preview / reset until it reconnects.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=0)
    reg = Registry()
    cs, ws = _connect(reg)
    cs.pending_snapshot_id = "req-abandoned"
    cs.pending_sent_at = _now_ms() - 60_000        # a minute old => abandoned

    task = asyncio.create_task(ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=1000)))
    for _ in range(200):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert ws.sent, "an abandoned slot must be reclaimable"
    assert cs.pending_snapshot_id != "req-abandoned"
    assert cs.pending_snapshot_id.startswith("req-")

    await _set_snapshot_at(db, "i1", _now_ms())
    fresh, reason, _out = await task
    assert (fresh, reason) == (True, FRESH)


async def test_sends_its_own_request_when_the_slot_is_free(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=0)
    reg = Registry()
    cs, ws = _connect(reg)

    task = asyncio.create_task(ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=1000)))
    for _ in range(200):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert [f["type"] for f in ws.sent] == ["snapshot_request"]
    await _set_snapshot_at(db, "i1", _now_ms())
    fresh, reason, _out = await task
    assert (fresh, reason) == (True, FRESH)


async def test_already_fresh_costs_nothing(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=_now_ms())
    reg = Registry()
    cs, ws = _connect(reg)
    fresh, reason, out = await ensure_fresh(reg, db, "i1", _settings())
    assert (fresh, reason) == (True, FRESH) and out is cs
    assert ws.sent == []


async def test_no_socket_is_disconnected_and_a_silent_instance_times_out(tmp_path):
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=0)
    reg = Registry()
    # No ConnState at all.
    assert await ensure_fresh(reg, db, "i1", _settings()) == (False, DISCONNECTED, None)

    # Connected but never answers => TIMEOUT (the caller decides what that means: a 409
    # for restore, «не учтён» for preview).
    _connect(reg)
    fresh, reason, out = await ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=150))
    assert (fresh, reason, out) == (False, TIMEOUT, None)


async def test_session_mismatch_is_never_fresh(tmp_path):
    # §5: the mirror was taken under a different session than the live socket, so it
    # describes a browser that no longer exists.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="OLD", snapshot_at=_now_ms())
    reg = Registry()
    _connect(reg, session="NEW")
    fresh, reason, _out = await ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=150))
    assert fresh is False and reason == TIMEOUT


async def test_the_reclaim_horizon_outlasts_a_pass_still_waiting(tmp_path):
    """The reclaim margin, pinned. ``runner._request_all_snapshots`` stamps
    ``pending_sent_at`` per instance INSIDE its send loop but ``runner._await_ready``
    only starts its deadline AFTER that loop finishes — so the pass keeps waiting on the
    FIRST instance until ``sent_at + timeout + <send fan-out>``. A 1× horizon could
    therefore fire while the pass still considers itself waiting, which is precisely the
    ejection this module exists to prevent, so the horizon is 2×.

    Reddens if ``_RECLAIM_AFTER_TIMEOUTS`` drops back to 1: at 1.5× elapsed the slot
    would be claimed here instead of still being respected."""
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=0)
    reg = Registry()
    cs, ws = _connect(reg)
    timeout_ms = 400
    # 1.5 timeouts old: PAST 1×, so a 1× horizon would reclaim — but a pass whose send
    # loop took a moment may still be waiting, so we must not.
    cs.pending_snapshot_id = "pass-abc"
    cs.pending_sent_at = _now_ms() - int(timeout_ms * 1.5)

    task = asyncio.create_task(
        ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=timeout_ms))
    )
    await asyncio.sleep(0.15)
    assert cs.pending_snapshot_id == "pass-abc"
    assert ws.sent == []
    await _set_snapshot_at(db, "i1", _now_ms())
    fresh, reason, _out = await task
    assert (fresh, reason) == (True, FRESH)
    assert ws.sent == []

    # Past 2× the SAME slot IS reclaimable — the wedge-breaker still works.
    await _set_snapshot_at(db, "i1", 0)
    cs.pending_snapshot_id = "pass-abc"
    cs.pending_sent_at = _now_ms() - timeout_ms * 3
    task2 = asyncio.create_task(
        ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=timeout_ms))
    )
    for _ in range(200):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert ws.sent, "a slot past the reclaim horizon must be claimable"
    await _set_snapshot_at(db, "i1", _now_ms())
    await task2


async def test_budget_caps_the_total_wait(tmp_path):
    """``budget_ms`` bounds BOTH rounds, not each one.

    Without it the worst case is ``(_RECLAIM_AFTER_TIMEOUTS + 1) ×`` the timeout — right
    for restore (an honest refusal is worth the wait) and wrong for preview, which §8
    lets mark an instance «не учтён» and move on. A rule edit runs the preview inline and
    DELETE runs it twice, so an unbudgeted 3× per instance turns one button into a
    reverse-proxy 504.
    """
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=0)
    reg = Registry()
    cs, ws = _connect(reg)
    cs.pending_snapshot_id = "pass-frozen"      # a foreign request, freshly sent
    cs.pending_sent_at = _now_ms()

    started = time.monotonic()
    fresh, reason, _out = await ensure_fresh(
        reg, db, "i1", _settings(snapshot_timeout_ms=400), budget_ms=200
    )
    elapsed = time.monotonic() - started

    assert (fresh, reason) == (False, TIMEOUT)
    # Inside its budget (with slack for the 50ms poll tick) and nowhere near the 3×1.2s
    # an unbudgeted call would have spent.
    assert elapsed < 0.6, f"budget ignored: waited {elapsed:.2f}s"
    # The budget is spent WAITING, never by clobbering the foreign slot to save time.
    assert cs.pending_snapshot_id == "pass-frozen"
    assert ws.sent == []


async def test_budget_still_allows_its_own_request_when_the_slot_is_free(tmp_path):
    # A budget must not turn into "never ask": with a free slot the request goes out and
    # the answer is awaited within the budget.
    db = await _make_db(tmp_path)
    await _insert_instance(db, "i1", session_id="s1", snapshot_at=0)
    reg = Registry()
    cs, ws = _connect(reg)

    task = asyncio.create_task(
        ensure_fresh(reg, db, "i1", _settings(snapshot_timeout_ms=2000), budget_ms=2000)
    )
    for _ in range(200):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert [f["type"] for f in ws.sent] == ["snapshot_request"]
    await _set_snapshot_at(db, "i1", _now_ms())
    fresh, reason, _out = await task
    assert (fresh, reason) == (True, FRESH)
