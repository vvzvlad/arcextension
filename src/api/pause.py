"""``POST /api/pause`` + ``DELETE /api/pause`` — the emergency-stop write side (§7).

The READ side (the pass step-1 gate, the pause-suppressed metrics, ``paused_until`` in
``/api/state`` and ``list_instances``) already exists; these two verbs ARM and LIFT the
pause. Both converge on :mod:`src.curator.pause` — the single write-shape shared with
the MCP ``pause`` / ``resume`` tools.

* ``POST /api/pause {minutes?}`` — arm or extend a pause. ``minutes`` defaults to
  ``PAUSE_DEFAULT_MIN`` and is clamped to a finite window (a pause is never infinite,
  §7). Extending while paused is allowed, so this route is deliberately NOT behind the
  pause gate. Returns ``{paused_until, pause_started_at}``.
* ``DELETE /api/pause`` — manual resume. Shifts the TTL protections by the ACTUAL pause
  duration, clears the pause + the ``resume_pending`` latch, THEN triggers a real pass
  immediately: a human at the keyboard wants the backlog handled now, unlike a timeout
  expiry which defers behind a click (§7). Also NOT gated (it is a resume verb).

authed via ``require_api_caller`` (Bearer ADMIN_TOKEN or an instance secret — §35 §4);
both refuse degraded mode (``require_operational``).
"""

from __future__ import annotations

import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_api_caller, require_operational
from src.curator import pause as pause_ops
from src.curator import runner
from src.db.settings_store import get_setting


def _now_ms() -> int:
    return int(time.time() * 1000)


async def pause_endpoint(request: Request) -> JSONResponse:
    """``POST /api/pause`` — arm/extend a finite pause. NOT behind the pause gate."""
    await require_api_caller(request)
    require_operational(request)

    body: dict = {}
    if await request.body():
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="request body must be JSON")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        body = data

    minutes = body.get("minutes")
    # A bool is an int subclass in Python — reject it explicitly, then any other
    # non-int; a valid int is range-clamped inside pause.py (never infinite).
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)):
        raise HTTPException(status_code=422, detail="minutes must be an integer")

    app = request.app
    default = app.state.settings.pause_default_min
    mins = pause_ops.clamp_minutes(minutes, default)
    now = _now_ms()

    until = await app.state.db.write(lambda c: pause_ops.pause(c, now=now, minutes=mins))
    started_raw = await app.state.db.read(
        lambda c: get_setting(c, pause_ops.PAUSE_STARTED_AT_KEY)
    )
    started = int(started_raw) if started_raw not in (None, "") else now
    return JSONResponse({"paused_until": until, "pause_started_at": started})


async def resume_now(app) -> dict:
    """Manual resume: TTL shift + clear the latch, THEN run a pass immediately (§7).

    THE resume shape, shared by ``DELETE /api/pause`` and the MCP ``resume`` tool. §7
    is explicit that a manual resume runs a pass at once («Снятие руками
    (`DELETE /api/pause`) запускает проход немедленно») and that only a TIMEOUT expiry
    defers behind a click. The MCP tool used to stop after the settings write, so an
    agent's resume left the curator idle until the next tick — the same verb with two
    behaviours. Returns ``{ttl_shift_ms, pass}``.
    """
    now = _now_ms()
    shift = await app.state.db.write(lambda c: pause_ops.resume(c, now=now))
    # Human (or agent) asked for it → handle the backlog immediately (§7).
    #
    # ``confirm_pending=True`` is REQUIRED, not decorative. Clearing the pause is not
    # enough to get past step 1: an armed ``resume_pending`` latch makes an unconfirmed
    # pass return ``{"status": "resume_pending"}` before it does anything, and the latch
    # is armed by a CONTINUITY BREAK too — not only by an expired pause. In that case
    # there is no pause to clear, the stored fingerprint is refreshed only by a real
    # pass, and a real pass is exactly what the latch is blocking: pressing the button
    # cleared nothing, ran nothing, and re-armed on the next tick. The state had no exit.
    #
    # Passing it unconditionally is safe: with no armed latch the runner logs and
    # degrades it to an ordinary pass (see ``confirm_pending and not resume_armed``).
    result = await runner.run_pass(
        app.state.db,
        app.state.ext_registry,
        app.state.settings,
        confirm_pending=True,
        clock_guard=getattr(app.state, "curator_clock", None),
    )
    return {"ttl_shift_ms": shift, "pass": result}


async def resume_endpoint(request: Request) -> JSONResponse:
    """``DELETE /api/pause`` — manual resume (TTL shift + clear) then run a pass now."""
    await require_api_caller(request)
    require_operational(request)

    outcome = await resume_now(request.app)
    return JSONResponse({"resumed": True, **outcome})
