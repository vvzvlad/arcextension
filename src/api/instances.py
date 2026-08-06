"""``POST /api/instances/:id/merge_windows`` — the manual «слить окна сейчас» (§9, §10).

§9 keeps a manual escape hatch next to the automatic step-9 merge: «Кнопка "слить окна
сейчас" на стартпейдже и MCP-инструмент остаются — для случая, когда ждать час не
хочется». The MCP half existed; this is the HTTP half the startpage button needs, and
both now run the SAME :func:`merge_windows` core (the MCP tool delegates here) so the
button and the agent cannot drift apart.

Server-side this is an ordinary command path (:mod:`src.ext.commands`): send
``merge_windows`` to the instance, surface the §6 error code. The §9 GUARDS — only
normal, non-fullscreen windows, nothing on screen or audible, all tabs idle past
``IDLE_MINUTES`` — are enforced by the EXTENSION at the edge, which is where the live
window/tab state actually is; the server does not re-derive them for a manual click.

**Pause (§7).** The merge is automation, so an armed pause silences it — EXCEPT with an
explicit ``{"force": true}``: §9 calls this «Кнопка "слить окна сейчас" на стартпейдже»,
and §7's one exception is exactly «собственные кнопки человека, и то с явным
`force:true`». The exception is HTTP-only: the MCP tool has no ``force`` argument and no
way to reach one, because an agent is not a human at the keyboard and the paused system
exists precisely to stop it (§7/§12).

A completed merge is journaled as ``actions(kind='window_merge')`` — the same kind the
pass's step 9 writes, with ``initiator`` naming who asked (``user`` for the button,
``mcp`` for the tool) and ``pass_id`` NULL (it belongs to no pass). Without a row the
manual merge would be the one tab-moving operation in the system outside the journal,
and §7 requires a forced action to be recorded as ``initiator=user`` besides. A REFUSED
merge writes nothing: §9 treats ``busy_dragging`` &co. as "retry later, not a failure",
and nothing moved. §9 still marks a merge non-undoable, so the row is archival only.
"""

from __future__ import annotations

import json
import sqlite3
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import (
    initiator_for,
    read_force_body,
    require_api_caller,
    require_not_paused,
    require_operational,
)
from src.db.actions import insert_action
from src.ext import protocol
from src.ext.commands import CommandError, send_command

# §6 codes that mean "your picture of the windows is stale / there is nothing to fold"
# — answered 409 + refetch (the same contract ``/api/focus`` uses), not 502.
_CLIENT_ERRORS = frozenset({protocol.ERR_NO_WINDOW, protocol.ERR_BUSY_DRAGGING})


def _record_merge(
    conn: sqlite3.Connection, instance_id: str, *, initiator: str, merged: int,
    forced: bool, now: int,
) -> int:
    """One ``actions(kind='window_merge')`` row for a completed MANUAL merge (§9/§7).

    ``pass_id`` stays NULL — this belongs to no pass, which also keeps it out of
    ``curator_actions_last_pass{kind}`` (that gauge is scoped to the newest ``pass_id``)
    and out of ``undo``, which only ever walks one pass's rows. ``detail`` follows the
    pass's ``window_merge`` payload shape (JSON) and carries the ``force`` marker, so a
    forced action is tellable apart in the archive using the EXISTING column — the same
    discipline ``restore:force`` / ``undo_close:force`` use, no new column (§7).
    """
    detail: dict = {"manual": True, "merged": merged}
    if forced:
        detail["force"] = True
    return insert_action(
        conn,
        ts=now,
        kind="window_merge",
        status="done",
        initiator=initiator,
        instance_from=instance_id,
        detail=json.dumps(detail),
    )


async def merge_windows(app, instance_id: str, params: dict | None = None,
                        *, initiator: str = "user", auth_ctx: str | None = None,
                        forced: bool = False, expected_session: str | None = None) -> dict:
    """Fold ``instance_id``'s windows into one; return ``{"merged": <int>}``.

    THE shared core: the HTTP endpoint below and the MCP ``merge_windows`` tool both
    call it, so the guard order, the params shape and the answer are defined once.
    Empty ``params`` is the manual "merge all" (§9): every other normal window folds
    into the focused one. Raises :class:`~src.ext.commands.CommandError` on a §6
    failure — the callers map it onto their own transport.

    ``forced`` is ARCHIVAL ONLY: it marks the journal row as "done through an armed
    pause". It performs no gate check of its own, so calling the core cannot bypass a
    pause — the gate lives in each caller (``require_not_paused`` here,
    ``_ensure_not_paused`` on the MCP side, which has no force at all).
    """
    settings = app.state.settings
    result = await send_command(
        app.state.ext_registry,
        app.state.db,
        instance_id,
        protocol.CMD_MERGE_WINDOWS,
        params or {},
        cmd_timeout_ms=settings.cmd_timeout_ms,
        initiator=initiator,
        auth_ctx=auth_ctx,
        # #47: the MCP tool may pin the session it read; the HTTP button passes None
        # (the human is at the keyboard now, stamp the live session as before).
        expected_session=expected_session,
    )
    # The extension answers {merged: <count of tabs moved>}; a missing/garbled value
    # becomes 0 rather than None so the documented `{"merged": <int>}` contract holds.
    raw = result.get("merged")
    merged = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0
    await app.state.db.write(
        lambda c: _record_merge(
            c, instance_id, initiator=initiator, merged=merged, forced=forced,
            now=int(time.time() * 1000),
        )
    )
    return {"merged": merged}


async def merge_windows_endpoint(request: Request) -> JSONResponse:
    """``POST /api/instances/:id/merge_windows`` → ``200 {"merged": <int>}`` (§10)."""
    caller = await require_api_caller(request)  # 401 before anything else
    require_operational(request)    # 503 in degraded mode
    # A merge is automation the pause silences (§7) — unless the human clicked the §9
    # button with an explicit force:true. Body BEFORE the gate so the flag is visible.
    # force is honoured only for the instance caller (the human); an admin's force
    # cannot cross the pause (§35 §4). initiator: 'user' (instance) / 'admin' (§35 §5).
    body = await read_force_body(request)
    forced = body.get("force") is True and caller.kind == "instance"
    await require_not_paused(request, force=forced)

    instance_id = request.path_params["instance_id"]
    try:
        return JSONResponse(
            await merge_windows(
                request.app, instance_id, initiator=initiator_for(caller), forced=forced
            )
        )
    except CommandError as exc:
        status_code = 409 if exc.code in _CLIENT_ERRORS else 502
        return JSONResponse(
            {"ok": False, "error": exc.code, "message": exc.message,
             "refetch": exc.code in _CLIENT_ERRORS},
            status_code=status_code,
        )
