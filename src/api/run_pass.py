"""``POST /api/run_pass`` — trigger one curator pass (§7).

Body (all optional): ``{dry_run?: bool, confirm_pending?: bool}``. ``dry_run`` takes
the lease, requests snapshots and returns the plan WITHOUT writing actions and is NOT
muted by a pause (looking at the plan is exactly why a pause is taken).
``confirm_pending`` confirms the deferred plan armed by a pause expiry or a
continuity break. Bearer ``EXT_TOKEN``; refuses degraded mode.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_ext_token, require_operational
from src.curator import runner


async def run_pass_endpoint(request: Request) -> JSONResponse:
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

    app = request.app
    result = await runner.run_pass(
        app.state.db,
        app.state.ext_registry,
        app.state.settings,
        dry_run=bool(body.get("dry_run")),
        confirm_pending=bool(body.get("confirm_pending")),
        clock_guard=getattr(app.state, "curator_clock", None),
    )
    return JSONResponse(result)
