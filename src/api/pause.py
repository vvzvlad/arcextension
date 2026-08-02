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

Bearer ``EXT_TOKEN``; both refuse degraded mode (``require_operational``).
"""

from __future__ import annotations

import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_ext_token, require_operational
from src.curator import pause as pause_ops
from src.curator import runner
from src.db.settings_store import get_setting


def _now_ms() -> int:
    return int(time.time() * 1000)


async def pause_endpoint(request: Request) -> JSONResponse:
    """``POST /api/pause`` — arm/extend a finite pause. NOT behind the pause gate."""
    require_ext_token(request)
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


async def resume_endpoint(request: Request) -> JSONResponse:
    """``DELETE /api/pause`` — manual resume (TTL shift + clear) then run a pass now."""
    require_ext_token(request)
    require_operational(request)

    app = request.app
    now = _now_ms()
    shift = await app.state.db.write(lambda c: pause_ops.resume(c, now=now))

    # Human at the keyboard → handle the backlog immediately (§7). The pause is now
    # cleared, so this real (non-dry, non-confirm) pass runs normally past step 1.
    result = await runner.run_pass(
        app.state.db,
        app.state.ext_registry,
        app.state.settings,
        clock_guard=getattr(app.state, "curator_clock", None),
    )
    return JSONResponse({"resumed": True, "ttl_shift_ms": shift, "pass": result})
