"""``POST /api/pause`` + ``DELETE /api/pause`` — the emergency-stop write side (§7).

The READ side (the pass step-1 gate, the stop-suppressed metrics, ``stopped_at`` in
``/api/state`` and ``list_instances``) already exists; these two verbs STOP and START
the curator. Both converge on :mod:`src.curator.pause` — the single write-shape shared
with the MCP ``pause`` / ``resume`` tools.

* ``POST /api/pause`` — stop the curator INDEFINITELY (there is no duration and no
  expiry; the only way back is the start verb). Idempotent: a re-press keeps the
  original stop time, so the start-side TTL shift reflects the full stop duration.
  Deliberately NOT behind the stop gate. Returns ``{stopped_at}``.
* ``DELETE /api/pause`` — start. Shifts the TTL protections by the ACTUAL stop
  duration, clears the stop, THEN triggers a real pass immediately: a human at the
  keyboard wants the backlog handled now (§7). Whether that pass CONFIRMS an armed
  over-threshold latch depends on whether an actual stop was lifted — see
  :func:`resume_now`. Also NOT gated (it is the start verb).

authed via ``require_api_caller`` (Bearer ADMIN_TOKEN or an instance secret — §35 §4);
both refuse degraded mode (``require_operational``).
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_api_caller, require_operational
from src.curator import pause as pause_ops
from src.curator import runner


def _now_ms() -> int:
    return int(time.time() * 1000)


async def pause_endpoint(request: Request) -> JSONResponse:
    """``POST /api/pause`` — stop the curator indefinitely. NOT behind the stop gate.

    Any request body is ignored: the stop has no parameters (the former timed pause
    took ``minutes``; the stop is indefinite by design, so there is nothing to clamp
    and nothing to validate).
    """
    await require_api_caller(request)
    require_operational(request)

    now = _now_ms()
    app = request.app
    stopped_at = await app.state.db.write(lambda c: pause_ops.stop(c, now=now))
    return JSONResponse({"stopped_at": stopped_at})


async def resume_now(app) -> dict:
    """Start the curator: TTL shift + clear the stop, THEN a pass now (§7).

    THE start shape, shared by ``DELETE /api/pause`` and the MCP ``resume`` tool. §7
    is explicit that a manual start runs a pass at once («Старт (`DELETE /api/pause`)
    запускает проход немедленно»). The MCP tool used to stop after the settings
    write, so an agent's resume left the curator idle until the next tick — the same
    verb with two behaviours. Returns ``{ttl_shift_ms, pass}``.

    The ONE verb means TWO things, and the confirm flag follows the meaning, not the
    verb:

    * **Старт** — the DELETE arrives while a stop is armed. It resumes automation
      through the NORMAL threshold gate (``confirm_pending=False``): the stopped UI
      shows the stop row, not the latched plan, so a latch armed under the stop was
      never on the owner's screen and the start click must not pre-approve it. If
      the plan recomputed by this pass is over the threshold, the pass latches and
      RETURNS it — the UI then shows the plan, and the next click («Выполнить», a
      DELETE with no stop set) confirms informed.
    * **The latch-confirm click** — the DELETE arrives with NO stop armed. The plan
      was rendered right next to the button, so ``confirm_pending=True``: this is
      the owner's informed "yes, do it", executing the plan recomputed at click
      time.

    A pass that never got to run (a re-pressed stop, an unavailable lease, a clock
    step) leaves the latch armed — which is the truth: its plan is still pending, and
    the button can be pressed again.
    """
    now = _now_ms()

    def _start(conn):
        # Read the stop BEFORE the shift: ``apply_resume_shift`` clears
        # ``curator_stopped_at``, so reading it afterwards always answers "not
        # stopped" and every start would silently confirm. Same transaction, so no
        # stop can slip in between the read and the shift.
        was_stopped = pause_ops.read_stopped_at(conn) is not None
        return was_stopped, pause_ops.apply_resume_shift(conn, now=now)

    # The ``resume_pending`` latch is deliberately NOT touched here (the runner owns
    # it): the pass below has to still SEE it. A confirming pass consumes it; a
    # start's normal-gated pass either executes an under-threshold plan (and clears
    # the latch itself, in ``runner._clear_deferred_plan``) or refreshes it for the
    # informed second click.
    was_stopped, shift = await app.state.db.write(_start)
    # Human (or agent) asked for it → handle the backlog immediately (§7).
    result = await runner.run_pass(
        app.state.db,
        app.state.ext_registry,
        app.state.settings,
        confirm_pending=not was_stopped,
        clock_guard=getattr(app.state, "curator_clock", None),
    )
    return {"ttl_shift_ms": shift, "pass": result}


async def resume_endpoint(request: Request) -> JSONResponse:
    """``DELETE /api/pause`` — start (TTL shift + clear the stop) then run a pass now."""
    await require_api_caller(request)
    require_operational(request)

    outcome = await resume_now(request.app)
    return JSONResponse({"resumed": True, **outcome})
