"""Starlette application factory for the curator service.

Phase 2 exposes only ``GET /healthz`` (liveness). The lifespan opens the
:class:`~src.db.access.Database` (which runs migrations), records degraded state,
and — when healthy — starts the nightly backup loop. A migration failure must NOT
crash startup: the app still serves so that a later /metrics can export
``curator_migration_failed=1`` and /healthz keeps liveness green (§12).
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from loguru import logger
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from src.api.actions import list_actions
from src.api.cors import CountingCORSMiddleware, cors_kwargs
from src.api.guards import require_operational
from src.api.metrics import metrics
from src.api.quick_links import quick_links_ops
from src.api.restore import restore_action
from src.api.state import focus, get_state
from src.api.rules import (
    create_rule,
    delete_rule,
    list_rules,
    preview_rule,
    reset_rule,
    update_rule,
)
from src.api.run_pass import run_pass_endpoint
from src.api.undo import undo_pass
from src.curator import runner
from src.curator.clock import ClockGuard
from src.db.access import Database
from src.db.backup import nightly_backup_loop
from src.db.retention import retention_loop
from src.ext.channel import ext_channel
from src.ext.registry import Registry
from src.mcpiface.server import build_mcp, mcp_route

# Re-exported so callers (and tests) keep importing it from src.app; the
# implementation lives in src.api.guards to avoid an app<->endpoint import cycle.
__all__ = ["create_app", "healthz", "require_operational"]


async def healthz(request: Request) -> JSONResponse:
    # Liveness ONLY — never reflect curation/migration health here (§12).
    return JSONResponse({"status": "ok"})


async def _curator_driver(app, db, settings) -> None:
    """Periodic driver: one curator pass every ``PASS_INTERVAL_MIN`` (§7).

    The lease (with its fencing epoch) is the real guard against concurrent passes,
    so this loop needs no lock of its own. A pass failure is logged and the loop
    continues — never let one bad pass stop the driver.
    """
    interval = settings.pass_interval_min * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await runner.run_pass(
                db, app.state.ext_registry, settings,
                clock_guard=app.state.curator_clock,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad pass must not kill the driver
            logger.exception("curator driver: pass failed")


def create_app(settings) -> Starlette:
    # Build the MCP server ONCE, before the lifespan (§11 trap #1: streamable_http_app()
    # creates the session_manager lazily and must be called before it is accessed).
    # The tools read `app_ref.app.state` at call time; `app_ref.app` is filled in below
    # once the Starlette app exists (by then the lifespan has populated app.state).
    app_ref = SimpleNamespace(app=None)
    mcp = build_mcp(app_ref)

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
        # The server-clock guard (§7) is a persistent monotonic-vs-wall comparator
        # shared by the periodic driver and POST /api/run_pass; the lease is the real
        # single-run guard, so sharing one guard across both callers is safe.
        app.state.curator_clock = ClockGuard(settings.pass_interval_min * 60)

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
            # The curator pass driver: run one pass every PASS_INTERVAL_MIN. Only one
            # pass ever runs at a time — enforced by the fencing lease, not this loop.
            background_tasks.append(
                asyncio.create_task(_curator_driver(app, db, settings))
            )
        else:
            logger.warning(
                "degraded mode: nightly backup and retention loops not started"
            )

        try:
            # Enter the MCP session manager's task group for the serving window (§11
            # trap #2: a mounted sub-app's lifespan is NOT run by Starlette, so the
            # HOST lifespan must initialize it or the first /mcp request raises
            # "Task group is not initialized").
            async with mcp.session_manager.run():
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
        # Prometheus scrape (§12): SEPARATE METRICS_TOKEN Bearer, read-only, and
        # served even in degraded mode. Pass/instance gauges are computed at scrape
        # time from `passes`/`instances`, never from process memory.
        Route("/metrics", metrics, methods=["GET"]),
        # Startpage state + jump (§10). /api/state returns the mirror immediately and
        # kicks a background single-flight refresh; /api/focus raises a foreign tab.
        Route("/api/state", get_state, methods=["GET"]),
        Route("/api/focus", focus, methods=["POST"]),
        # Quick links offline op queue flush (§10): array of ops + Idempotency-Key.
        Route("/api/quick_links/ops", quick_links_ops, methods=["POST"]),
        # Archive list (§10): filtered, deferred hidden by default, newest-first.
        Route("/api/actions", list_actions, methods=["GET"]),
        Route(
            "/api/actions/{action_id:int}/restore",
            restore_action,
            methods=["POST"],
        ),
        # Pass undo (§10): restore per reversible row + close copies; per-row summary.
        Route("/api/passes/{pass_id}/undo", undo_pass, methods=["POST"]),
        # Rules engine (§8, §10). `/preview` and `/:id/reset` are declared before the
        # bare `/api/rules` so their distinct paths route unambiguously.
        Route("/api/rules", list_rules, methods=["GET"]),
        Route("/api/rules", create_rule, methods=["POST"]),
        Route("/api/rules/preview", preview_rule, methods=["POST"]),
        Route("/api/rules/{rule_id:int}", update_rule, methods=["PUT"]),
        Route("/api/rules/{rule_id:int}", delete_rule, methods=["DELETE"]),
        Route("/api/rules/{rule_id:int}/reset", reset_rule, methods=["POST"]),
        # Curator pass (§7): trigger one pass (dry_run / confirm_pending optional).
        Route("/api/run_pass", run_pass_endpoint, methods=["POST"]),
        # MCP over streamable HTTP (§11): an exact Route at /mcp (NOT a Mount under
        # /mcp, which would double the path to /mcp/mcp and add a 307). Auth is the
        # same EXT_TOKEN Bearer, enforced inside the ASGI handler.
        mcp_route(mcp),
        WebSocketRoute("/ext", ext_channel),
    ]
    # CORS for /api/* (§12): explicit chrome-extension:// origins only, NEVER '*'.
    # The middleware ignores non-http scopes (so /ext WebSocket is untouched) and
    # only echoes an origin present in the allow-list, so /metrics/healthz scrapes
    # (no Origin header) pass through unaffected. Same allow-list as the hello check.
    middleware = [
        Middleware(
            CountingCORSMiddleware, **cors_kwargs(settings.ext_allowed_origins)
        )
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=middleware)
    # Let the MCP tools reach app.state (db / ext_registry / settings) at call time.
    app_ref.app = app
    app.state.mcp = mcp
    return app
