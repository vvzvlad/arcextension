"""Out-of-pass snapshot freshness (§6) — ONE mechanism, every caller.

Restore (``/api/actions/:id/restore`` and pass undo), the rule preview (§8), the
manual ``reset`` (§8/§10) and ``/api/state``'s background kick all need the same
thing: "make this instance's mirror fresh, now". Each used to own a copy of the
poll loop, and all but ``/api/state``'s wrote ``ConnState.pending_snapshot_id``
UNCONDITIONALLY — which silently ejects an instance from a curator pass that is in
flight. The channel matches snapshot ids EXACTLY
(:func:`src.ext.channel._handle_snapshot`), so overwriting a pass's ``pass-<uuid>``
makes the instance's answer to the PASS be dropped, its
``last_applied_snapshot_id`` never matches what the pass sent, and the pass
excludes the instance without a word.

**The rule: never clobber a foreign ``pending_snapshot_id``.** A request is issued
only when nothing is in flight. When something IS in flight — ``pass-<uuid>`` from
the curator or ``req-<uuid>`` from another out-of-pass caller — we WAIT for its
result instead: whoever asked is refreshing the same mirror, and ``snapshot_at`` is
the shared "it landed" signal. A dropped / never-answered id self-clears when the
next snapshot is applied (the channel sets it back to ``None``), so a wedged
instance cannot block requests forever.

Callers keep their OWN semantics for "it never became fresh": restore raises 409
(never silently substitute ``main``), preview marks the instance «не учтён» and
carries on, reset refuses. This module only reports ``(fresh, reason, conn_state)``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid

from src.ext import protocol

# Reason strings returned by :func:`ensure_fresh` — short and machine-readable, so a
# caller can map them onto its own status code / payload without re-parsing prose.
FRESH = "fresh"
DISCONNECTED = "disconnected"
TIMEOUT = "timeout"

# Poll cadence while waiting for a snapshot to land: a fresh reader connection per
# tick, coarse enough to stay off the hot path at this scale.
_POLL_INTERVAL_S = 0.05

# How many ``SNAPSHOT_TIMEOUT_MS`` a foreign request must outlive before its slot may be
# reclaimed. NOT 1×, and the margin is load-bearing: a pass stamps ``pending_sent_at``
# per instance INSIDE ``runner._request_all_snapshots``' send loop, but only starts its
# ``runner._await_ready`` deadline AFTER that loop finishes. So the pass keeps waiting on
# the FIRST instance until ``sent_at + timeout + (time to send to all the others)`` —
# a small but unbounded skew, which makes a 1× test able to fire while the pass still
# considers itself waiting, i.e. exactly the ejection this module exists to prevent. 2×
# puts the reclaim a whole snapshot timeout past any plausible send fan-out while still
# bounding the "one unanswered request wedges the instance" failure it exists to break.
_RECLAIM_AFTER_TIMEOUTS = 2


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_snapshot_request_id() -> str:
    """``req-<uuid>`` — the out-of-pass snapshot request id (the pass uses ``pass-…``)."""
    return f"req-{uuid.uuid4()}"


def read_instance_freshness(conn: sqlite3.Connection, instance_id: str):
    """Reader ``fn(conn)``: ``(connected, session_id, snapshot_at)`` or ``None``."""
    return conn.execute(
        "SELECT connected, session_id, snapshot_at FROM instances WHERE id = ?",
        (instance_id,),
    ).fetchone()


async def is_fresh(db, instance_id: str, conn_state, settings) -> bool:
    """§6 out-of-pass freshness: connected, SAME session as the live socket, and a
    ``snapshot_at`` younger than ``STATE_FRESH_MS``."""
    row = await db.read(lambda c: read_instance_freshness(c, instance_id))
    if row is None:
        return False
    connected, session_id, snapshot_at = row[0], row[1], row[2]
    if not connected or snapshot_at is None:
        return False
    # The live socket's session must match what the mirror was taken under (§5).
    if session_id != conn_state.session_id:
        return False
    return (_now_ms() - snapshot_at) < settings.state_fresh_ms


async def request_snapshot(conn_state) -> None:
    """Send ONE ``snapshot_request`` and claim the ``pending_snapshot_id`` slot.

    Callers MUST check the slot is free first — see :func:`ensure_fresh`, which is
    the only place that should decide to claim it.
    """
    request_id = new_snapshot_request_id()
    conn_state.pending_snapshot_id = request_id
    conn_state.pending_sent_at = _now_ms()
    await conn_state.ws.send_json(
        {"type": protocol.TYPE_SNAPSHOT_REQUEST, "id": request_id}
    )


async def _poll_until_fresh(registry, db, instance_id, conn_state, settings, window_s: float):
    """Poll ``snapshot_at`` for ``window_s`` seconds. Returns a reason string."""
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_S)
        if registry.get(instance_id) is not conn_state:
            return DISCONNECTED  # socket superseded/dropped underneath us
        if await is_fresh(db, instance_id, conn_state, settings):
            return FRESH
    return TIMEOUT


def _remaining_s(pending_sent_at, timeout_ms: int) -> float:
    """How long a foreign in-flight request may still legitimately be answered.

    Its own sender gives up somewhere around ``SNAPSHOT_TIMEOUT_MS`` — but not EXACTLY
    at it (see :data:`_RECLAIM_AFTER_TIMEOUTS`), so the abandonment horizon carries a
    full extra timeout of margin. Only past that is the request ABANDONED rather than in
    flight. An unknown ``pending_sent_at`` is treated as "just sent" — the cautious
    reading, since claiming the slot early is the failure mode we are avoiding.
    """
    horizon_ms = timeout_ms * _RECLAIM_AFTER_TIMEOUTS
    if pending_sent_at is None:
        return horizon_ms / 1000.0
    remaining_ms = horizon_ms - (_now_ms() - pending_sent_at)
    if remaining_ms <= 0:
        return 0.0
    return min(remaining_ms, horizon_ms) / 1000.0


async def ensure_fresh(registry, db, instance_id: str, settings, *, budget_ms: int | None = None):
    """Make ``instance_id``'s mirror fresh, without ejecting it from a running pass.

    Returns ``(fresh: bool, reason: str, conn_state | None)`` where ``reason`` is
    :data:`FRESH`, :data:`DISCONNECTED` (no live socket, or the socket was superseded
    mid-poll) or :data:`TIMEOUT` (connected but the mirror did not become fresh).

    Two rounds, and the split is the whole point:

    1. **A foreign request is already in flight** → wait out ITS remaining lifetime
       instead of issuing a competing one. Its answer refreshes the same mirror, and
       ``snapshot_at`` is the shared signal; overwriting the id would eject the
       instance from a pass (module docstring).
    2. **Nothing in flight, or the foreign request is ABANDONED** → send ours and wait a
       full window. "Abandoned" means older than ``_RECLAIM_AFTER_TIMEOUTS ×
       SNAPSHOT_TIMEOUT_MS``, by which point whoever sent it has certainly stopped
       waiting, so reclaiming the slot ejects nobody — and without this an extension
       that never answers ONE request would wedge every later restore / preview / reset
       until it reconnected.

    The claim is made synchronously right after the check (``request_snapshot`` assigns
    before its first ``await``), so two concurrent callers cannot both claim the slot.

    ``budget_ms`` caps the TOTAL wait across both rounds. Without it the worst case is
    ``(_RECLAIM_AFTER_TIMEOUTS + 1) × SNAPSHOT_TIMEOUT_MS`` — 30 s at the defaults — which
    is right for restore (it owes the human an honest refusal, and a wrong answer is
    worse than a slow one) and wrong for preview, which §8 explicitly allows to give up
    early and mark an instance «не учтён». Callers that can degrade should pass a budget.
    """
    conn_state = registry.get(instance_id)
    if conn_state is None:
        return (False, DISCONNECTED, None)
    if await is_fresh(db, instance_id, conn_state, settings):
        return (True, FRESH, conn_state)

    timeout_ms = settings.snapshot_timeout_ms
    deadline = None if budget_ms is None else time.monotonic() + budget_ms / 1000.0

    def _window(seconds: float) -> float:
        """Clamp a round's wait to whatever is left of the caller's budget."""
        if deadline is None:
            return seconds
        return max(0.0, min(seconds, deadline - time.monotonic()))

    foreign = conn_state.pending_snapshot_id
    if foreign is not None:
        reason = await _poll_until_fresh(
            registry, db, instance_id, conn_state, settings,
            _window(_remaining_s(conn_state.pending_sent_at, timeout_ms)),
        )
        if reason == FRESH:
            return (True, FRESH, conn_state)
        if reason == DISCONNECTED:
            return (False, DISCONNECTED, None)
        # The foreign request outlived the abandonment horizon without landing. Claim
        # the slot only if it is STILL that same abandoned id — a different id means
        # somebody already took over, and a fresh request of theirs must not be
        # clobbered either.
        if conn_state.pending_snapshot_id not in (None, foreign):
            return (False, TIMEOUT, None)
        if deadline is not None and time.monotonic() >= deadline:
            return (False, TIMEOUT, None)   # budget spent waiting on the foreign request

    try:
        await request_snapshot(conn_state)
    except Exception:  # noqa: BLE001 - a dead socket is a disconnect, not a 500
        return (False, DISCONNECTED, None)

    reason = await _poll_until_fresh(
        registry, db, instance_id, conn_state, settings, _window(timeout_ms / 1000.0)
    )
    return (reason == FRESH, reason, conn_state if reason == FRESH else None)
