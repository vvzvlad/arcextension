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
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute

from src.api.actions import list_actions
from src.api.admin import (
    close_enroll_window_endpoint,
    get_enroll_window,
    list_instances as admin_list_instances,
    open_enroll_window,
    revoke as admin_revoke,
)
from src.api.admin_page import (
    AdminSecurityHeadersMiddleware,
    admin_page,
    app_css,
    app_js,
    login_js,
    login_page,
    login_submit,
    logout_submit,
)
from src.api.cors import CountingCORSMiddleware, cors_kwargs
from src.api.exemptions import create_exemption, delete_exemption, list_exemptions
from src.api.guards import require_operational
from src.api.instances import merge_windows_endpoint
from src.api.metrics import metrics
from src.api.pause import pause_endpoint, resume_endpoint
from src.api.quick_links import quick_links_ops
from src.api.restore import restore_action
from src.api.state import focus, get_state
from src.api.rules import (
    create_rule,
    delete_rule,
    get_rule,
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


async def _http_exception(request: Request, exc: HTTPException) -> Response:
    """HTTPException renderer: a DICT ``detail`` becomes a JSON body, everything else
    keeps Starlette's plain-text default. The pause gate (``require_not_paused``)
    raises 423 with ``{"error":"paused","until":<ms>}`` — a structured body the client
    reads — while existing string-detail 4xx/5xx responses render unchanged (§7/§12)."""
    if exc.status_code in {204, 304}:
        return Response(status_code=exc.status_code, headers=exc.headers)
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)
    return PlainTextResponse(
        exc.detail, status_code=exc.status_code, headers=exc.headers
    )


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
        # /ext admission counters (§2), initialized here so every reader sees a number
        # from the first request rather than relying on a getattr default: sockets holding
        # a pre-auth slot, and the subset of those that have not yet sent a first frame
        # (which governs the first-frame deadline — see src.ext.channel).
        app.state.ext_preauth_count = 0
        app.state.ext_silent_count = 0
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
            # (There used to be a third loop here: a TICK_MS sweep that physically deleted
            # expired `enroll_requests`. Enrolment is one step now — a request never
            # becomes a row that could go stale — so the table, its TTL and the sweep are
            # all gone, §6.)
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
        Route("/api/rules/{rule_id:int}", get_rule, methods=["GET"]),
        Route("/api/rules/{rule_id:int}", update_rule, methods=["PUT"]),
        Route("/api/rules/{rule_id:int}", delete_rule, methods=["DELETE"]),
        Route("/api/rules/{rule_id:int}/reset", reset_rule, methods=["POST"]),
        # Exemptions (§10): the human's «не трогать до …», the same table the pass's
        # step-4 guards read and restore has always written to.
        Route("/api/exemptions", list_exemptions, methods=["GET"]),
        Route("/api/exemptions", create_exemption, methods=["POST"]),
        Route("/api/exemptions", delete_exemption, methods=["DELETE"]),
        # Manual window merge (§9/§10): the startpage's «слить окна сейчас» button;
        # the same core the MCP merge_windows tool runs. -> {"merged": <int>}.
        Route(
            "/api/instances/{instance_id}/merge_windows",
            merge_windows_endpoint,
            methods=["POST"],
        ),
        # Curator pass (§7): trigger one pass (dry_run / confirm_pending optional).
        Route("/api/run_pass", run_pass_endpoint, methods=["POST"]),
        # Pause (§7): POST arms/extends a finite pause; DELETE resumes (TTL shift +
        # an immediate pass). Both are exceptions to the pause gate (resume verbs).
        Route("/api/pause", pause_endpoint, methods=["POST"]),
        Route("/api/pause", resume_endpoint, methods=["DELETE"]),
        # Enrollment JSON API (§13). ADMIN-only (ADMIN_TOKEN / MCP): the operator opens /
        # reads / closes the enrollment window and lists/revokes instances. There is no
        # approve/reject pair and no pending list — an enroll_request with a valid code
        # into an open window enrols itself over /ext (§6). JSON only — the HTML console
        # (#36) renders this API. Reads are allowed in degraded mode; the mutating verbs
        # (revoke / window arm+close) answer 503 while degraded.
        # /admin HTML console (§13, issue #36) — the presentation layer over the JSON API
        # above. Serving is by EXPLICIT handlers (never StaticFiles) so every HTML/asset
        # response can carry the CSP header. GET /admin is auth-gated (cookie OR Bearer);
        # the login form + .js/.css assets are public and hold no secrets. Login mints a
        # random-id cookie session (in-memory); logout drops it.
        Route("/admin", admin_page, methods=["GET"]),
        Route("/admin/login", login_page, methods=["GET"]),
        Route("/admin/login", login_submit, methods=["POST"]),
        Route("/admin/logout", logout_submit, methods=["POST"]),
        Route("/admin/app.js", app_js, methods=["GET"]),
        Route("/admin/app.css", app_css, methods=["GET"]),
        Route("/admin/login.js", login_js, methods=["GET"]),
        Route("/admin/enroll/window", open_enroll_window, methods=["POST"]),
        Route("/admin/enroll/window", get_enroll_window, methods=["GET"]),
        Route("/admin/enroll/window", close_enroll_window_endpoint, methods=["DELETE"]),
        Route("/admin/instances", admin_list_instances, methods=["GET"]),
        Route(
            "/admin/instances/{instance_id}/revoke",
            admin_revoke,
            methods=["POST"],
        ),
        # MCP over streamable HTTP (§11): an exact Route at /mcp (NOT a Mount under
        # /mcp, which would double the path to /mcp/mcp and add a 307). Auth is the
        # ADMIN_TOKEN Bearer (the agent equals the human, §35), enforced inside the ASGI handler.
        mcp_route(mcp),
        WebSocketRoute("/ext", ext_channel),
    ]
    # CORS for /api/* (§12): ANY origin, credentials off — the lock on /api/* is
    # require_api_caller, not the browser's origin check (see src/api/cors.py for why the
    # old allow-list was removed). The middleware ignores non-http scopes (so the /ext
    # WebSocket is untouched) and /metrics/healthz scrapes (no Origin header) are
    # unaffected either way.
    middleware = [
        Middleware(CountingCORSMiddleware, **cors_kwargs()),
        # Stamp X-Content-Type-Options: nosniff on every /admin response (HTML/asset AND
        # the JSON API). Header-only, so it never alters a JSON body/status (§13, #36).
        Middleware(AdminSecurityHeadersMiddleware),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=middleware,
        exception_handlers={HTTPException: _http_exception},
    )
    # Let the MCP tools reach app.state (db / ext_registry / settings) at call time.
    app_ref.app = app
    app.state.mcp = mcp
    return app
