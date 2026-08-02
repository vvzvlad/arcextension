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
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from src.api.guards import require_operational
from src.api.restore import restore_action
from src.api.rules import (
    create_rule,
    delete_rule,
    list_rules,
    preview_rule,
    reset_rule,
    update_rule,
)
from src.db.access import Database
from src.db.backup import nightly_backup_loop
from src.db.retention import retention_loop
from src.ext.channel import ext_channel
from src.ext.registry import Registry

# Re-exported so callers (and tests) keep importing it from src.app; the
# implementation lives in src.api.guards to avoid an app<->endpoint import cycle.
__all__ = ["create_app", "healthz", "require_operational"]


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

        background_tasks: list[asyncio.Task] = []
        if not result.degraded:
            background_tasks.append(asyncio.create_task(nightly_backup_loop(db)))
            # Sibling periodic task: prune old actions / js_audit (§12). Separate,
            # longer horizon for js_audit is enforced inside run_retention.
            background_tasks.append(
                asyncio.create_task(
                    retention_loop(
                        db,
                        settings.actions_retention_days,
                        settings.js_audit_retention_days,
                    )
                )
            )
        else:
            logger.warning(
                "degraded mode: nightly backup and retention loops not started"
            )

        try:
            yield
        finally:
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await db.close()

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route(
            "/api/actions/{action_id:int}/restore",
            restore_action,
            methods=["POST"],
        ),
        # Rules engine (§8, §10). `/preview` and `/:id/reset` are declared before the
        # bare `/api/rules` so their distinct paths route unambiguously.
        Route("/api/rules", list_rules, methods=["GET"]),
        Route("/api/rules", create_rule, methods=["POST"]),
        Route("/api/rules/preview", preview_rule, methods=["POST"]),
        Route("/api/rules/{rule_id:int}", update_rule, methods=["PUT"]),
        Route("/api/rules/{rule_id:int}", delete_rule, methods=["DELETE"]),
        Route("/api/rules/{rule_id:int}/reset", reset_rule, methods=["POST"]),
        WebSocketRoute("/ext", ext_channel),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
