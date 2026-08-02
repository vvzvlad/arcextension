"""``GET /api/state`` + ``POST /api/focus`` (§10).

* ``GET /api/state`` returns the ``StateResponse`` mirror IMMEDIATELY (§10) and, as
  a Starlette background task (i.e. AFTER the response is flushed), kicks a
  per-instance SINGLE-FLIGHT snapshot refresh for every connected instance whose
  mirror is older than ``STATE_FRESH_MS``. It NEVER blocks on a fan-out of snapshot
  responses (the single-threaded writer, §4): the response is the current mirror,
  the refresh only benefits the NEXT open. The single-flight flag lives on the
  ``ConnState`` so a burst of newtab opens fires ONE request per instance.

* ``POST /api/focus`` sends a ``focus_tab`` command to the instance (§10/§14.1: it
  activates the tab and raises its own window). ``no_such_tab`` is surfaced as a
  clear 409 so the page re-fetches ``/api/state`` and re-renders — never silent.

All routes: Bearer ``EXT_TOKEN`` then ``require_operational``; all snapshot/command
I/O is async and OUTSIDE any DB transaction (Фаза 2 contract).
"""

from __future__ import annotations

import asyncio
import time

from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.freshness import is_fresh, request_snapshot
from src.api.guards import (
    read_force_body,
    require_ext_token,
    require_not_paused,
    require_operational,
)
from src.db import state as state_read
from src.ext import protocol
from src.ext.commands import CommandError, send_command


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- background single-flight refresh ---------------------------------------
def _is_stale(snapshot_at, now: int, state_fresh_ms: int) -> bool:
    """A connected instance's mirror is stale when it has never been snapshotted or
    the last snapshot is older than ``STATE_FRESH_MS`` (§10)."""
    if snapshot_at is None:
        return True
    return (now - snapshot_at) >= state_fresh_ms


async def _clear_when_settled(registry, db, instance_id, conn_state, settings) -> None:
    """Poll until the refreshed snapshot lands (mirror fresh again) or the request
    times out, then ALWAYS clear the single-flight flag. Runs detached — the
    endpoint never awaits it, so a slow/never-answering instance cannot delay
    ``/api/state`` (§10). Reuses the §6 out-of-pass freshness check.
    """
    try:
        deadline = time.monotonic() + settings.snapshot_timeout_ms / 1000.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if registry.get(instance_id) is not conn_state:
                return  # socket superseded/dropped underneath us
            if await is_fresh(db, instance_id, conn_state, settings):
                return
    except Exception:  # noqa: BLE001 - a detached refresh must never crash the loop
        pass
    finally:
        conn_state.state_refresh_inflight = False


async def kick_state_refresh(app, db, settings) -> None:
    """Kick a per-instance single-flight snapshot refresh for stale, connected
    instances (§10). Sends the request(s) — fast, local, single-flight-gated — and
    detaches the wait, so this returns without awaiting any snapshot RESPONSE.

    The flag is set BEFORE the ``await`` on the send so a second concurrent kick
    (the next Cmd+T) observes it and does NOT send a duplicate.
    """
    registry = app.state.ext_registry
    now = _now_ms()
    ages = await db.read(state_read.connected_snapshot_ages)
    tasks: set = getattr(app.state, "state_refresh_tasks", None)
    if tasks is None:
        tasks = app.state.state_refresh_tasks = set()
    for instance_id, snapshot_at in ages.items():
        conn_state = registry.get(instance_id)
        # Skip when a request is ALREADY in flight — the single-flight flag (a prior
        # kick) OR a live ``pending_snapshot_id``. That id may belong to a curator
        # pass (``pass-<uuid>``, §7) or a restore (``req-<uuid>``, restore.py): the
        # kick must NOT clobber it. Overwriting a pass's id ejects the instance from
        # that pass — the instance's answer to the pass id is dropped (the channel
        # matches ids exactly), ``last_applied_snapshot_id`` never matches, and it is
        # silently excluded. This guard is now THE shared rule, factored into
        # :mod:`src.api.freshness` and obeyed by restore / preview / reset too. A
        # dropped/never-answered pending id self-clears on the next applied snapshot
        # (the channel sets it None), so kicks resume. Read+decide SYNCHRONOUSLY (no
        # await before the guard below sets its flag), same discipline as
        # ``state_refresh_inflight``.
        if (
            conn_state is None
            or conn_state.state_refresh_inflight
            or conn_state.pending_snapshot_id is not None
        ):
            continue
        if not _is_stale(snapshot_at, now, settings.state_fresh_ms):
            continue
        # Set the guard SYNCHRONOUSLY (before the send await) so single-flight holds.
        conn_state.state_refresh_inflight = True
        try:
            await request_snapshot(conn_state)
        except Exception:  # noqa: BLE001 - a dead socket must not wedge the flag
            conn_state.state_refresh_inflight = False
            continue
        task = asyncio.create_task(
            _clear_when_settled(registry, db, instance_id, conn_state, settings)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)


# --- GET /api/state ---------------------------------------------------------
async def get_state(request: Request) -> JSONResponse:
    require_ext_token(request)      # 401 before anything else
    require_operational(request)    # 503 in degraded mode

    app = request.app
    db = app.state.db
    settings = app.state.settings

    # Return the mirror IMMEDIATELY (§10). Read it in one reader connection.
    state = await db.read(lambda c: state_read.build_state(c, _now_ms()))

    # Kick the refresh AFTER the response is flushed (background task): the response
    # is the current mirror, never blocked on a snapshot fan-out (§10/§4).
    return JSONResponse(
        state,
        background=BackgroundTask(kick_state_refresh, app, db, settings),
    )


# --- POST /api/focus --------------------------------------------------------
async def focus(request: Request) -> JSONResponse:
    require_ext_token(request)
    require_operational(request)

    # No `if not body` shortcut: `{}` is a well-formed JSON object that simply lacks the
    # fields, so it must fail the SAME way as `{"instance": ""}` — 422 naming the missing
    # field, not 400 "request body must be JSON". A truly absent body lands there too,
    # which is the more useful of the two answers. Malformed JSON is still 400, inside
    # `read_force_body`.
    body = await read_force_body(request)
    # A paused curator silences focus too (§7) — unless the human clicked with an
    # explicit force:true. Jumping to a tab is one of the human's own buttons.
    await require_not_paused(request, force=body.get("force") is True)

    instance_id = body.get("instance")
    tab_id = body.get("tabId")
    if not isinstance(instance_id, str) or not instance_id:
        raise HTTPException(status_code=422, detail="instance is required")
    if not isinstance(tab_id, int) or isinstance(tab_id, bool):
        raise HTTPException(status_code=422, detail="tabId (integer) is required")

    app = request.app
    settings = app.state.settings
    try:
        await send_command(
            app.state.ext_registry,
            app.state.db,
            instance_id,
            protocol.CMD_FOCUS_TAB,
            {"tabId": tab_id},
            cmd_timeout_ms=settings.cmd_timeout_ms,
            initiator="user",
        )
    except CommandError as exc:
        # no_such_tab is a CLEAR error the page acts on: re-fetch /api/state and
        # re-render (the mirror is stale — the tab is gone), never silent (§10).
        if exc.code == protocol.ERR_NO_SUCH_TAB:
            return JSONResponse(
                {"ok": False, "error": exc.code, "refetch": True}, status_code=409
            )
        # Any other failure (no live socket, timeout, no_window, stale_session):
        # the fallback "switch to <instance> manually" stays in the UI (§10). 502.
        return JSONResponse(
            {"ok": False, "error": exc.code, "message": exc.message}, status_code=502
        )

    return JSONResponse({"ok": True})
