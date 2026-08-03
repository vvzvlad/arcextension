"""``/admin/*`` — the JSON enrollment API an operator (or the MCP agent) drives (§13).

This is the JSON control surface of enrollment: list/approve/reject pending requests,
list/revoke instances, and open/read/close the enrollment window. The HTML console that
renders it is a SEPARATE issue (#36) — this module serves JSON ONLY (no templates, no
StaticFiles).

Auth (issue #35 §4). ``/admin`` is ADMIN-only: :func:`require_admin` runs the shared
``require_api_caller`` and then rejects an INSTANCE caller with 401 (acceptance 6). The
MCP agent bears ADMIN_TOKEN, so it is an admin caller too. Every endpoint gates on this
first. MUTATING endpoints additionally call ``require_operational`` (503 while the DB is
degraded — a migration failure means authoritative writes cannot be trusted); READ
endpoints do NOT, so the operator can still inspect the fleet in degraded mode.

All SQL lives in :mod:`src.db.queries` / :mod:`src.curator.enroll` / :mod:`src.db.retention`
(slices A/B/D) — the handlers only orchestrate reads/writes and map outcomes to HTTP.
``origin`` / ``suggested_title`` are UNTRUSTED (length already clamped server-side in
slice B) and returned VERBATIM: the JSON API never HTML-encodes; the #36 page textContent-s
them.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import (
    Caller,
    read_force_body,
    require_api_caller,
    require_operational,
)
from src.curator.enroll import arm_enroll_window, close_enroll_window, read_enroll_window
from src.db import queries
from src.db.queries import ApproveConflict, revoke_instance
from src.db.queries import RevokeMainRefused


# A bounded charset/length for the operator-assigned instance_id (the row PRIMARY KEY).
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


async def require_admin(request: Request) -> Caller:
    """Authenticate an ``/admin/*`` request as an ADMIN caller; 401 otherwise (§4, acc 6).

    Reuses :func:`src.api.guards.require_api_caller` (missing/invalid Bearer → 401, an
    instance secret → an instance :class:`Caller`, a DB outage → 503) and then rejects a
    non-admin: an instance secret on ``/admin/*`` is 401, never a silent pass. Kept thin —
    the auth itself is NOT duplicated, only the admin-kind narrowing lives here.
    """
    caller = await require_api_caller(request)
    if caller.kind != "admin":
        # An active-instance secret authenticated, but /admin is ADMIN-only (§4). 401,
        # the same status an unknown token gets, so an instance cannot probe /admin.
        raise HTTPException(status_code=401, detail="admin token required")
    return caller


def _ttl_ms(request: Request) -> int:
    """The enroll_request TTL in ms (ENROLL_REQUEST_TTL_MIN), the read-time cutoff base."""
    return request.app.state.settings.enroll_request_ttl_min * 60_000


# --- pending enroll requests -------------------------------------------------
async def list_enroll_requests(request: Request) -> JSONResponse:
    """``GET /admin/enroll/requests`` — the pending, NOT-expired requests (acc 12).

    Read-only, so it is allowed in degraded mode. The TTL filter is applied at read time
    (a request past ``ENROLL_REQUEST_TTL_MIN`` is never returned), independent of the
    physical sweep that later deletes it.
    """
    await require_admin(request)
    now = _now_ms()
    rows = await request.app.state.db.read(
        lambda c: queries.list_pending_enroll_requests(c, now=now, ttl_ms=_ttl_ms(request))
    )
    return JSONResponse({"requests": rows})


def _require_str(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    return value


async def approve(request: Request) -> JSONResponse:
    """``POST /admin/enroll/approve`` — enroll a pending request (§1, acceptance 4/11).

    Body ``{install_uuid, instance_id, [title]}``. The operator assigns ``instance_id``
    (stable across re-issue, §1). Flow:

    1. READ the pending request (own read txn) → 404 if absent/expired, and capture its
       ``secret_hash``. Reading it out of band is what lets two racing approves BOTH hold
       the secret so the write-side guards (not a 404) decide the loser.
    2. ONE write txn: create-or-reactivate the ``instances`` row + delete the consumed
       request + write the ``admin_audit`` row — all committed together. The two 409
       guards: an already-active id (``ApproveConflict``) and a ``UNIQUE(secret_hash)``
       collision (``sqlite3.IntegrityError``). Exactly one of two racing approves commits.

    Approval sends NOTHING to the extension — it learns via a successful secret-hello on
    its next alarm (acceptance 4). Returns ``{instance_id, status:'active'}``.
    """
    await require_admin(request)
    require_operational(request)
    body = await read_force_body(request)
    install_uuid = _require_str(body, "install_uuid")
    instance_id = _require_str(body, "instance_id")
    # instance_id becomes the row's PRIMARY KEY; the operator is trusted, but a bounded
    # charset/length keeps a fat-fingered or pasted value from becoming a permanent key.
    if not _INSTANCE_ID_RE.match(instance_id):
        raise HTTPException(
            status_code=400,
            detail="instance_id must be 1-64 chars of [A-Za-z0-9._-]",
        )
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="title must be a string")

    now = _now_ms()
    db = request.app.state.db
    req = await db.read(
        lambda c: queries.get_enroll_request(
            c, install_uuid, now=now, ttl_ms=_ttl_ms(request)
        )
    )
    if req is None:
        raise HTTPException(
            status_code=404, detail="no pending enroll request for that install_uuid"
        )
    # Body title wins; otherwise the client's suggested title (untrusted, already clamped).
    final_title = title if title is not None else req["suggested_title"]
    secret_hash = req["secret_hash"]

    def _txn(c: sqlite3.Connection) -> None:
        # Upsert (create-or-reactivate) + delete request + audit, atomically. A conflict
        # rolls the WHOLE txn back — no partial row, no audit for a failed approve, and the
        # request survives for a retry.
        queries.approve_enroll_request(
            c,
            instance_id=instance_id,
            secret_hash=secret_hash,
            install_uuid=install_uuid,
            title=final_title,
            now=now,
        )
        queries.insert_admin_audit(
            c,
            now=now,
            action="approve",
            initiator="admin",
            install_uuid=install_uuid,
            instance_id=instance_id,
            detail=json.dumps({"title": final_title}),
        )

    try:
        await db.write(_txn)
    except ApproveConflict:
        raise HTTPException(status_code=409, detail="instance id is already active")
    except sqlite3.IntegrityError:
        # UNIQUE(secret_hash): another active instance already carries this secret — the
        # concurrency loser (a DIFFERENT id, same request).
        raise HTTPException(status_code=409, detail="secret is already enrolled")
    return JSONResponse({"instance_id": instance_id, "status": "active"})


async def reject(request: Request) -> JSONResponse:
    """``POST /admin/enroll/reject`` — delete a pending request. Body ``{install_uuid}``.

    Idempotent: a 200 even when the request is already gone (the desired end state —
    request absent — already holds). The reject is always audited, with a ``deleted`` flag
    recording whether a row was actually present.
    """
    await require_admin(request)
    require_operational(request)
    body = await read_force_body(request)
    install_uuid = _require_str(body, "install_uuid")
    now = _now_ms()

    def _txn(c: sqlite3.Connection) -> bool:
        deleted = queries.reject_enroll_request(c, install_uuid)
        queries.insert_admin_audit(
            c,
            now=now,
            action="reject",
            initiator="admin",
            install_uuid=install_uuid,
            detail=json.dumps({"deleted": deleted}),
        )
        return deleted

    deleted = await request.app.state.db.write(_txn)
    return JSONResponse({"install_uuid": install_uuid, "rejected": True, "deleted": deleted})


# --- instances ---------------------------------------------------------------
async def list_instances(request: Request) -> JSONResponse:
    """``GET /admin/instances`` — every instance, all statuses (read-only, degraded-ok).

    Unlike the curator's active-only reads, this shows revoked/pending rows too so the
    operator can see a revoked MAIN awaiting re-approval or a stuck pending id.
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
