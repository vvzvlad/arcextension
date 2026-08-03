"""Wire an MCP server (streamable HTTP) into the EXISTING Starlette app (§11).

THE mount trap (§11), and exactly how this module avoids each half:

1. ``streamable_http_app()`` creates the ``session_manager`` LAZILY. Accessing
   ``mcp.session_manager`` before that call raises ``RuntimeError: Session manager
   can only be accessed after calling streamable_http_app()``. So :func:`build_mcp`
   calls it ONCE, at app-construction time, BEFORE the lifespan runs.
2. A mounted sub-app's lifespan is NOT run by Starlette, so the session manager's
   task group is never created -> ``RuntimeError: Task group is not initialized``
   on the first request. Fixed by entering ``mcp.session_manager.run()`` in the
   HOST app's lifespan (see :func:`src.app.create_app`). ``stateless_http=True`` does
   NOT fix this (the check precedes the branch).
3. ``streamable_http_app()`` already registers ``/mcp`` internally, so mounting that
   app under ``/mcp`` DOUBLES the path (``/mcp/mcp``). We do NOT mount the returned
   app: we take its ASGI handler (``StreamableHTTPASGIApp`` over the session manager)
   and expose it as a single ``Route('/mcp', …)`` on the host — the path is exactly
   ``/mcp``. A ``Route`` (not a ``Mount``) also avoids the trailing-slash 307 a Mount
   would add; the SDK follows redirects anyway, but an exact route is cleaner.

``mcp.run(transport="streamable-http")`` is unusable — it starts its own uvicorn and
takes the port; the service needs ``/ext``, ``/api/*``, ``/healthz``, ``/metrics`` on
the same port. The SDK version is NOT pinned (the 307-redirect concern that once
motivated a pin is false: the client hardcodes ``follow_redirects=True``).

Auth: ``/mcp`` sits behind the ``ADMIN_TOKEN`` Bearer (issue #35 §4: the agent equals
the human — both authenticate with ADMIN_TOKEN, which opens ``/mcp`` and ``/admin/*``).
The check runs in the ASGI wrapper before the request reaches the session
manager. DNS-rebinding Host/Origin validation is DISABLED: the endpoint is
Bearer-gated and reached server-to-server (no browser origin), behind Traefik — the
protection guards browser-driven localhost servers, which this is not, and enabling
it would couple the service to its deployment hostname.
"""

from __future__ import annotations

import contextvars
import secrets

from mcp.server import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from src.api.auth_metrics import auth_rejections
from src.mcpiface import tools

# The streamable-HTTP session id of the CURRENT request, set by the ASGI wrapper
# from the ``Mcp-Session-Id`` header. execute_js records it as ``auth_ctx`` so the
# js_audit trace names the MCP SESSION, not a token id (§12).
_mcp_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_session", default=None
)


def current_mcp_session() -> str | None:
    """The MCP session id bound to the in-flight request (or None before initialize)."""
    return _mcp_session.get()


# --- the MCP server + its tools ---------------------------------------------
def build_mcp(app_ref) -> MCPServer:
    """Build the MCPServer and register every §11 tool.

    ``app_ref`` is a holder whose ``.app`` is set to the host Starlette app once it
    exists; the tool wrappers read ``app_ref.app.state`` at CALL time (by which point
    the lifespan has populated ``db`` / ``ext_registry`` / ``settings``). The handler
    bodies live in :mod:`src.mcpiface.tools` and are unit-tested directly.
    """
    mcp = MCPServer("arc-curator")

    def _host():
        return app_ref.app

    async def _guarded(coro):
        """Run a handler coroutine; turn a ``ToolError`` into a structured result the
        agent can read (a refusal or a preview is data, not a transport failure)."""
        # §12 parity with /api/*: a degraded schema (a failed migration) is not trusted
        # for reads OR writes — every /api/* endpoint returns 503 via require_operational,
        # the curator driver does not start, and the MCP layer must refuse too so an
        # agent cannot mutate against an unverified schema or grab the lease.
        host = app_ref.app
        if host is not None and getattr(host.state, "degraded", False):
            coro.close()  # the handler body never ran; don't leave it un-awaited
            return {"ok": False, "error": "degraded",
                    "message": "service is in degraded mode (§12); mutations and reads refused"}
        try:
            return await coro
        except tools.ToolError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message, **exc.payload}
        except HTTPException as exc:
            # A reused /api helper (e.g. list_actions' filter parser) may raise 422 —
            # surface it as structured data, not a transport failure.
            return {"ok": False, "error": f"http_{exc.status_code}", "message": exc.detail}

    # --- reads ---------------------------------------------------------------
    @mcp.tool()
    async def list_instances() -> dict:
        """List instances with per-instance snapshot_at and paused_until (§11)."""
        return await _guarded(tools.list_instances(_host()))

    @mcp.tool()
    async def list_tabs() -> dict:
        """List all mirrored tabs plus per-instance snapshot_at (§11 freshness)."""
        return await _guarded(tools.list_tabs(_host()))

    @mcp.tool()
    async def get_rules() -> dict:
        """List the curation rules (§8)."""
        return await _guarded(tools.get_rules(_host()))

    @mcp.tool()
    async def list_actions(
        kind: str | None = None, pass_id: str | None = None,
        instance: str | None = None, url: str | None = None, since: int | None = None,
        deferred: bool | None = None, limit: int | None = None, offset: int | None = None,
    ) -> dict:
        """List archive actions with the same filters as GET /api/actions (§10)."""
        return await _guarded(tools.list_actions(
            _host(), kind=kind, pass_id=pass_id, instance=instance, url=url,
            since=since, deferred=deferred, limit=limit, offset=offset,
        ))

    # --- rule writes (confirm_impact gate) -----------------------------------
    @mcp.tool()
    async def upsert_rule(rule: dict, confirm_impact: bool = False) -> dict:
        """Create/update a rule; momentous changes require confirm_impact=True (§8)."""
        return await _guarded(tools.upsert_rule(_host(), rule=rule, confirm_impact=confirm_impact))

    @mcp.tool()
    async def delete_rule(rule_id: int, confirm_impact: bool = False) -> dict:
        """Delete a rule; always requires confirm_impact=True after the preview (§8)."""
        return await _guarded(tools.delete_rule(_host(), rule_id=rule_id, confirm_impact=confirm_impact))

    @mcp.tool()
    async def reset_singleton(rule_id: int) -> dict:
        """Return a rule's canonical_url reset target (§8/§10)."""
        return await _guarded(tools.reset_singleton(_host(), rule_id=rule_id))

    # --- commands (initiator='mcp' + auth_ctx = MCP session) -----------------
    @mcp.tool()
    async def open_tab(instance: str, url: str, pinned: bool = False, active: bool = False) -> dict:
        """Open a tab in an instance (§6)."""
        return await _guarded(tools.open_tab(
            _host(), instance=instance, url=url, pinned=pinned, active=active,
            auth_ctx=current_mcp_session(),
        ))

    @mcp.tool()
    async def close_tab(instance: str, tab_id: int) -> dict:
        """Close a tab in an instance (§6)."""
        return await _guarded(tools.close_tab(
            _host(), instance=instance, tab_id=tab_id, auth_ctx=current_mcp_session()
        ))

    @mcp.tool()
    async def focus_tab(instance: str, tab_id: int) -> dict:
        """Focus a tab and raise its window (§6/§10)."""
        return await _guarded(tools.focus_tab(
            _host(), instance=instance, tab_id=tab_id, auth_ctx=current_mcp_session()
        ))

    @mcp.tool()
    async def merge_windows(instance: str, params: dict | None = None) -> dict:
        """Merge windows in an instance (§9)."""
        return await _guarded(tools.merge_windows(
            _host(), instance=instance, params=params, auth_ctx=current_mcp_session()
        ))

    @mcp.tool()
    async def execute_js(
        instance: str, tab_id: int, code: str,
        world: str | None = None, url_at_exec: str | None = None,
    ) -> dict:
        """Run JS in a tab (§12: audited before send, gated by the checkbox+kill-switch)."""
        return await _guarded(tools.execute_js(
            _host(), instance=instance, tab_id=tab_id, code=code, world=world,
            url_at_exec=url_at_exec, auth_ctx=current_mcp_session(),
        ))

    @mcp.tool()
    async def relocate_tab(instance_from: str, tab_id: int, instance_to: str) -> dict:
        """Relocate a tab: phase-A open + a relocate row the pass's phase B completes (§7)."""
        return await _guarded(tools.relocate_tab(
            _host(), instance_from=instance_from, tab_id=tab_id, instance_to=instance_to,
            auth_ctx=current_mcp_session(),
        ))

    # --- pass + pause --------------------------------------------------------
    @mcp.tool()
    async def run_pass(dry_run: bool = False) -> dict:
        """Trigger one curator pass; dry_run returns the plan without writing (§7)."""
        return await _guarded(tools.run_pass(_host(), dry_run=dry_run))

    @mcp.tool()
    async def pause(minutes: int | None = None) -> dict:
        """Pause the curator (writes pause_until + bumps the epoch, §7/§12)."""
        return await _guarded(tools.pause(_host(), minutes=minutes))

    @mcp.tool()
    async def resume() -> dict:
        """Resume the curator (clears pause_until, §7/§12)."""
        return await _guarded(tools.resume(_host()))

    # Create the session manager LAZILY, ONCE, BEFORE the lifespan (§11 trap #1).
    # DNS-rebinding protection off — see the module docstring.
    mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return mcp


# --- the auth-gated ASGI handler + its Route --------------------------------
class _MCPAsgi:
    """ASGI app: enforce the ``ADMIN_TOKEN`` Bearer, bind the MCP session id, then hand
    off to the streamable-HTTP handler. A class instance (not a bare function) so a
    Starlette ``Route`` treats it as an ASGI app rather than a request endpoint."""

    def __init__(self, session_manager) -> None:
        # The SAME ASGI handler the SDK's own streamable_http_app wraps in its Route.
        self._asgi = StreamableHTTPASGIApp(session_manager)

    async def __call__(self, scope, receive, send) -> None:
        headers = dict(scope.get("headers") or [])
        settings = scope["app"].state.settings
        auth = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = auth.partition(" ")
        # Constant-time compare, as bytes, against ADMIN_TOKEN (issue #35 §4: the MCP
        # agent authenticates as the human/admin, not with EXT_TOKEN).
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.encode("utf-8", "ignore"), settings.admin_token.encode("utf-8")
        ):
            # Count the rejection for curator_auth_rejections_total (§12).
            auth_rejections.incr("mcp_token")
            await PlainTextResponse("missing or invalid bearer token", status_code=401)(
                scope, receive, send
            )
            return
        # Bind the streamable-HTTP session id for this request so execute_js can record
        # it as auth_ctx (§12). Absent on the initialize request; present thereafter.
        session_id = headers.get(b"mcp-session-id", b"").decode("latin-1") or None
        reset = _mcp_session.set(session_id)
        try:
            await self._asgi(scope, receive, send)
        finally:
            _mcp_session.reset(reset)


def mcp_route(mcp: MCPServer) -> Route:
    """The single host-app route for the MCP endpoint — path exactly ``/mcp``."""
    return Route(
        "/mcp",
        _MCPAsgi(mcp.session_manager),
        methods=["GET", "POST", "DELETE"],
    )
