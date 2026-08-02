"""Starlette application factory for the curator service.

Phase 2 exposes only ``GET /healthz`` (liveness). The lifespan opens the
:class:`~src.db.access.Database` (which runs migrations), records degraded state,
and — when healthy — starts the nightly backup loop. A migration failure must NOT
crash startup: the app still serves so that a later /metrics can export
``curator_migration_failed=1`` and /healthz keeps liveness green (§12).
"""

import asyncio
from contextlib import asynccontextmanager

from loguru import logger
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from src.db.access import Database
from src.db.backup import nightly_backup_loop
from src.ext.channel import ext_channel
from src.ext.registry import Registry


def require_operational(request: Request) -> None:
    """Raise 503 when the service is in degraded mode.

    Later phases' mutating endpoints (``/api/*``, ``/ext``) call this so they
    refuse to act on a database whose migrations failed or that was migrated by
    newer code. ``/healthz`` deliberately does NOT call it — liveness must stay
    green so an orchestrator keeps routing to the container (§12).
    """
    if getattr(request.app.state, "degraded", False):
        raise HTTPException(status_code=503, detail="service degraded")


async def healthz(request: Request) -> JSONResponse:
    # Liveness ONLY — never reflect curation/migration health here (§12).
    return JSONResponse({"status": "ok"})


def create_app(settings) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        db = Database(settings.db_path, settings.backup_dir)
        result = await db.open()
        app.state.db = db
        app.state.settings = settings
        app.state.degraded = result.degraded
        app.state.migration_reason = result.reason
        # Per-process /ext state: the live-connection registry and the rejections
        # counter a later phase's /metrics exports (curator_auth_rejections_total).
        app.state.ext_registry = Registry()
        app.state.ext_rejections = 0

        backup_task: asyncio.Task | None = None
        if not result.degraded:
            backup_task = asyncio.create_task(nightly_backup_loop(db))
        else:
            logger.warning("degraded mode: nightly backup loop not started")

        try:
            yield
        finally:
            if backup_task is not None:
                backup_task.cancel()
                try:
                    await backup_task
                except asyncio.CancelledError:
                    pass
            await db.close()

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        WebSocketRoute("/ext", ext_channel),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
