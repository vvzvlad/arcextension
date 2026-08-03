"""``/admin/*`` — the JSON enrollment API an operator (or the MCP agent) drives (§13).

This is the JSON control surface of enrollment: list/revoke instances, and open/read/close
the enrollment window. The HTML console that renders it is a SEPARATE issue (#36) — this
module serves JSON ONLY (no templates, no StaticFiles).

There is deliberately NO approve/reject pair and no pending-request list. Enrolment is
one step: an ``enroll_request`` carrying a valid code into an OPEN window creates the
active instance in :mod:`src.ext.channel`, under the id its operator typed. The window is
the permission; approval existed only to assign an id the browser now brings, and the one
objection it really answered — a name collision — is refused loudly at the /ext frame
instead of costing a manual step on every ordinary enrolment. What is left here is what
the console still needs: arming the window (which mints the code), reading it, closing it
early, and the fleet list with revocation.

Auth (issue #35 §4). ``/admin`` is ADMIN-only: :func:`require_admin` runs the shared
``require_api_caller`` and then rejects an INSTANCE caller with 401 (acceptance 6). The
MCP agent bears ADMIN_TOKEN, so it is an admin caller too. Every endpoint gates on this
first. MUTATING endpoints additionally call ``require_operational`` (503 while the DB is
degraded — a migration failure means authoritative writes cannot be trusted); READ
endpoints do NOT, so the operator can still inspect the fleet in degraded mode.

All SQL lives in :mod:`src.db.queries` / :mod:`src.curator.enroll` — the handlers only
orchestrate reads/writes and map outcomes to HTTP.
"""

from __future__ import annotations

import json
import sqlite3
import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api import admin_session
from src.api.auth_metrics import auth_rejections
from src.api.guards import (
    Caller,
    _bearer_token,
    read_force_body,
    require_api_caller,
    require_operational,
    require_same_origin,
)
from src.curator.enroll import arm_enroll_window, close_enroll_window, read_enroll_window
from src.db import queries
from src.db.queries import revoke_instance
from src.db.queries import RevokeMainRefused


def _now_ms() -> int:
    return int(time.time() * 1000)


async def require_admin(request: Request) -> Caller:
    """Authenticate an ``/admin/*`` request as an ADMIN caller; 401 otherwise (§4, acc 6).

    Accepts EITHER credential (issue #36 extends #35's Bearer-only gate additively):

    * **(a) ``Authorization: Bearer <ADMIN_TOKEN>``** — the #35 path (curl / MCP / the
      startpage agent). Delegated to :func:`require_api_caller` (missing/invalid Bearer →
      401, an instance secret → an instance :class:`Caller` we reject with 401, a DB outage
      → 503) and narrowed to ``kind=='admin'``. A Bearer request carries NO ambient cookie,
      so browsers cannot auto-send it cross-site — it therefore SKIPS the CSRF gate (acc 7).
    * **(b) a valid session cookie** — the #36 HTML console. Validated against the in-memory
      store (:mod:`src.api.admin_session`): server-side expiry + revoke + fingerprint match
      (a rotated ADMIN_TOKEN kills it, acc 4). Because the cookie is AMBIENT, a cookie-
      authenticated MUTATING verb (POST/PUT/DELETE) must additionally clear the same-origin
      CSRF gate (:func:`require_same_origin`, acc 3); reads (GET) skip it.

    A request with a Bearer header takes the Bearer path (so a stray cookie can never
    downgrade a curl call into the CSRF-gated branch). Neither credential → 401 (acc 1).
    """
    # (a) Bearer wins when an Authorization header is present. No CSRF: no ambient cookie.
    if _bearer_token(request) is not None:
        caller = await require_api_caller(request)
        if caller.kind != "admin":
            # An active-instance secret authenticated, but /admin is ADMIN-only (§4). 401,
            # the same status an unknown token gets, so an instance cannot probe /admin.
            raise HTTPException(status_code=401, detail="admin token required")
        request.state.admin_auth = "bearer"
        return caller

    # (b) Session cookie. A valid cookie is an admin caller (the console operator).
    session_id = request.cookies.get(admin_session.COOKIE_NAME)
    if admin_session.validate(request.app, session_id):
        # CSRF gate: reject a cross-origin mutating cookie request (no-op on GET).
        require_same_origin(request)
        caller = Caller(kind="admin")
        request.state.caller = caller
        request.state.admin_auth = "cookie"
        return caller

    # Neither a Bearer nor a valid session cookie → 401 (acc 1). No body leak.
    auth_rejections.incr("admin_session")
    raise HTTPException(status_code=401, detail="admin authentication required")


# --- instances ---------------------------------------------------------------
async def list_instances(request: Request) -> JSONResponse:
    """``GET /admin/instances`` — every instance, all statuses (read-only, degraded-ok).

    Unlike the curator's active-only reads, this shows revoked rows too so the operator
    can see a revoked MAIN awaiting re-enrolment or a retired browser.
    """
    await require_admin(request)
    rows = await request.app.state.db.read(queries.list_instances)
    return JSONResponse({"instances": rows})


async def _close_live_socket(app, instance_id: str) -> None:
    """Best-effort close of a revoked instance's live socket AFTER commit (§5).

    Swallows every error: the status='revoked' write already blocks re-hello and commands
    (the §5 safety net), so the socket close is a courtesy, not a correctness dependency.
    """
    registry = app.state.ext_registry
    cs = registry.get(instance_id)
    if cs is None:
        return
    try:
        if cs.heartbeat_task is not None:
            cs.heartbeat_task.cancel()
        await cs.ws.close(code=1012)  # takeover/retire, matching the eviction path
        registry.remove_if_current(instance_id, cs)
    except Exception:  # noqa: BLE001 - best-effort; a dead socket / teardown slip must not
        # fail an already-committed revoke (the status='revoked' write is the real guard).
        pass


async def revoke(request: Request) -> JSONResponse:
    """``POST /admin/instances/{id}/revoke`` — revoke an instance (§5, acceptance 9).

    Body optional ``{replacement}``. Calls slice D's :func:`revoke_instance` in one write
    txn (which also audits on success), maps :class:`RevokeMainRefused` → 409 (revoking
    MAIN needs ``replacement == the current MAIN``, acc 9) and a no-such-row → 404, then —
    AFTER commit — best-effort closes the live socket. Returns ``{instance_id, status}``.
    """
    await require_admin(request)
    require_operational(request)
    body = await read_force_body(request)
    replacement = body.get("replacement")
    if replacement is not None and not isinstance(replacement, str):
        raise HTTPException(status_code=400, detail="replacement must be a string")

    instance_id = request.path_params["instance_id"]
    now = _now_ms()
    settings = request.app.state.settings

    def _txn(c: sqlite3.Connection):
        result = revoke_instance(
            c,
            instance_id,
            now=now,
            main_instance_id=settings.main_instance_id,
            replacement=replacement,
        )
        # Audit ONLY a real revoke (a 404 no-op writes nothing). The MAIN refusal raises
        # before any write, so it never reaches here.
        if result.revoked:
            queries.insert_admin_audit(
                c,
                now=now,
                action="revoke",
                initiator="admin",
                instance_id=instance_id,
                detail=json.dumps({"replacement": replacement, "was_main": result.was_main}),
            )
        return result

    try:
        result = await request.app.state.db.write(_txn)
    except RevokeMainRefused:
        raise HTTPException(
            status_code=409,
            detail="revoking MAIN requires replacement == the current MAIN_INSTANCE_ID",
        )
    if not result.revoked:
        raise HTTPException(status_code=404, detail="no such instance")

    await _close_live_socket(request.app, instance_id)
    return JSONResponse({"instance_id": instance_id, "status": "revoked"})


# --- enrollment window -------------------------------------------------------
async def open_enroll_window(request: Request) -> JSONResponse:
    """``POST /admin/enroll/window`` — arm the window; return ``{code, until, seconds_remaining}``.

    The operator reads ``code`` to type into an extension during enrollment. Mints a FRESH
    code every open (slice A). Audited as ``window_open``.
    """
    await require_admin(request)
    require_operational(request)
    now = _now_ms()
    minutes = request.app.state.settings.enroll_window_min

    def _txn(c: sqlite3.Connection):
        state = arm_enroll_window(c, now=now, minutes=minutes)
        queries.insert_admin_audit(
            c, now=now, action="window_open", initiator="admin",
            detail=json.dumps({"until": state.until}),
        )
        return state

    state = await request.app.state.db.write(_txn)
    return JSONResponse(
        {"code": state.code, "until": state.until, "seconds_remaining": state.seconds_remaining}
    )


async def get_enroll_window(request: Request) -> JSONResponse:
    """``GET /admin/enroll/window`` — read-time window state (read-only, degraded-ok).

    Returns ``{open, seconds_remaining}`` plus ``code`` ONLY while open (a closed/expired
    window never surfaces a dead code — slice A's symmetric state).
    """
    await require_admin(request)
    now = _now_ms()
    state = await request.app.state.db.read(lambda c: read_enroll_window(c, now=now))
    body = {"open": state.open, "seconds_remaining": state.seconds_remaining}
    if state.open:
        body["code"] = state.code
    return JSONResponse(body)


async def close_enroll_window_endpoint(request: Request) -> JSONResponse:
    """``DELETE /admin/enroll/window`` — close the window now. Audited as ``window_close``."""
    await require_admin(request)
    require_operational(request)
    now = _now_ms()

    def _txn(c: sqlite3.Connection) -> None:
        close_enroll_window(c)
        queries.insert_admin_audit(c, now=now, action="window_close", initiator="admin")

    await request.app.state.db.write(_txn)
    return JSONResponse({"open": False, "seconds_remaining": 0})
