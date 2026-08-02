"""Service -> extension commands: send, correlate by id, time out, surface codes.

This is the SERVICE half of §6 "Команды (сервис → расширение)". The extension
executes the verbs (§6 dispatcher); here we own the send + correlate + timeout +
error-code path, plus the ``execute_js`` audit-before-send rule and the runtime
kill-switch that can refuse an ``execute_js`` before it is ever sent (§12).

Correlation: :func:`send_command` builds a ``command {id, sessionId, command,
params}`` frame, stores an :class:`asyncio.Future` under ``id`` on the live
:class:`~src.ext.registry.ConnState`, and awaits it. The channel's receive loop
calls :func:`resolve_response` for every ``response {id, ...}`` frame, which
resolves the matching Future (or ignores an unknown/late id). ``asyncio.wait_for``
bounds the wait; the pending entry is always removed in a ``finally``.

All websocket I/O and every ``await`` here live OUTSIDE any DB transaction; the
only DB touch is the ``js_audit`` write, which is its own ``Database.write`` before
the send and another after the outcome is known (never awaited inside a txn).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger

from src.db.audit import insert_js_audit, update_js_audit_outcome
from src.db.queries import instance_status
from src.db.settings_store import is_execute_js_enabled
from src.ext import protocol


async def _safe_update_outcome(db, audit_id: int, outcome: str, detail: str | None) -> None:
    """Best-effort js_audit outcome update.

    A failure here (degraded DB, disk full mid-flight) must NOT mask the command's
    real result or the intended CommandError — the audit ROW itself was already
    durably committed before the send (§12), so the evidence is not lost even if
    the outcome column stays stale. Log and swallow.
    """
    try:
        await db.write(lambda c: update_js_audit_outcome(c, audit_id, outcome, detail))
    except Exception:  # noqa: BLE001 - never let an outcome-update fault mask the result
        logger.exception("failed to update js_audit outcome for audit {}", audit_id)


class CommandError(Exception):
    """A command failed. ``code`` is a §6 error code (or a service-side code).

    Carried verbatim from a ``response {ok:false, error:{code, message}}`` frame,
    or raised locally for ``no_connection`` / ``timeout``.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _new_command_id() -> str:
    # A uuid so a command id never collides with the snapshot_request ids
    # (``req-N``) that share the same socket.
    return f"cmd-{uuid.uuid4()}"


def resolve_response(conn_state, msg: dict[str, Any]) -> bool:
    """Resolve the pending command Future named by a ``response`` frame.

    Returns ``True`` when a pending id matched (the awaiting ``send_command`` is
    woken), ``False`` when the id is unknown/late/duplicate — such a frame is
    ignored (§6: a response whose id we are not waiting on addresses nothing).
    """
    rid = msg.get("id")
    if rid is None:
        return False
    fut = conn_state.pending_commands.get(rid)
    if fut is None or fut.done():
        # Unknown id, or a duplicate reply after the first already resolved it.
        return False
    fut.set_result(msg)
    return True


async def send_command(
    registry,
    db,
    instance_id: str,
    command: str,
    params: dict[str, Any],
    *,
    cmd_timeout_ms: int,
    initiator: str = "curator",
    auth_ctx: str | None = None,
) -> dict[str, Any]:
    """Send one command to ``instance_id`` and await its correlated response.

    Stamps ``sessionId`` = the instance's CURRENT ``session_id`` (from the live
    ``ConnState``) so the extension can reject a foreign session with
    ``stale_session`` (§5). Returns the ``result`` dict on ``ok:true``; raises
    :class:`CommandError` carrying the §6 code on ``ok:false``, on timeout, or when
    there is no live socket.

    For ``execute_js`` a ``js_audit`` row is written BEFORE the send (so rejected
    and timed-out executions are also recorded, §12) and its ``outcome`` is updated
    once known.
    """
    conn_state = registry.get(instance_id)
    if conn_state is None:
        # No live socket => nothing to send to. Never silently succeed.
        raise CommandError(
            protocol.ERR_NO_CONNECTION, f"no live connection to instance {instance_id}"
        )

    # §5 revoke safety net (issue #35): a command must not reach a REVOKED instance even
    # if the best-effort socket close after revoke lost the race to this send. When a DB
    # is available, a non-active target fails EXACTLY like a dead connection — the hello
    # ``status='active'`` guard (slice B) blocks re-enrollment, and this blocks in-flight
    # commands. A target with NO row at all is left to the registry/connection checks (a
    # live socket implies an approved row in production; the id-only test rigs carry no row
    # and keep behaving as before). Read-only, OUTSIDE any transaction, before the frame or
    # the execute_js audit — so a revoked target is refused with no side effect, like
    # ``no_connection``.
    if db is not None:
        status = await db.read(lambda c: instance_status(c, instance_id))
        if status is not None and status != "active":
            raise CommandError(
                protocol.ERR_NO_CONNECTION,
                f"instance {instance_id} is not active (status={status})",
            )

    request_id = _new_command_id()
    frame = {
        "type": protocol.TYPE_COMMAND,
        "id": request_id,
        # STAMP the current session so a stale/foreign session is rejected (§5).
        "sessionId": conn_state.session_id,
        "command": command,
        "params": params,
    }

    # execute_js MUST NOT run without a durable audit sink (§12): with no db to
    # write the js_audit row, refuse fail-closed rather than send arbitrary code
    # un-audited. The "JS ran without an audit row" code path must not exist.
    if command == protocol.CMD_EXECUTE_JS and db is None:
        raise CommandError(
            protocol.ERR_INTERNAL, "execute_js requires an audit sink (db is None)"
        )

    # execute_js: audit BEFORE sending, so a disabled/rejected/timed-out call is
    # still the only durable trace of arbitrary code execution (§12).
    audit_id: int | None = None
    if command == protocol.CMD_EXECUTE_JS and db is not None:
        # url_at_exec is NOT a §6 command param — the caller (a later MCP/pass phase)
        # passes `urlAtExec` in params when it knows the tab's URL, else it stays
        # NULL. The audit still records who/what/where via the other fields.
        audit_id = await db.write(
            lambda c: insert_js_audit(
                c,
                instance_id=instance_id,
                tab_id=params.get("tabId"),
                url_at_exec=params.get("urlAtExec"),
                world=params.get("world"),
                code=params.get("code") or "",
                initiator=initiator,
                auth_ctx=auth_ctx,
                now=_now_ms(),
            )
        )

        # Runtime kill-switch (§12 "запретить execute_js везде сейчас"): the
        # audit row is written FIRST (a refused call is still the only trace of an
        # execute_js attempt), THEN the switch is checked. When off we record
        # outcome='disabled' and REFUSE — no frame is ever put on the socket.
        if not await db.read(is_execute_js_enabled):
            # best-effort like every other outcome-update: a failure here must not
            # mask the intended CommandError (the audit row is already committed).
            await _safe_update_outcome(db, audit_id, "disabled", "kill_switch")
            raise CommandError(
                protocol.ERR_JS_DISABLED,
                "execute_js is disabled by the runtime kill-switch",
            )

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    conn_state.pending_commands[request_id] = fut
    try:
        try:
            await conn_state.ws.send_json(frame)
        except Exception as exc:  # noqa: BLE001 - socket closed between lookup and send
            # Honour the contract (only CommandError leaves this function) AND record
            # the audit outcome — a failed send of execute_js is still an attempt (§12).
            if audit_id is not None:
                await _safe_update_outcome(db, audit_id, "error", "send_failed")
            raise CommandError(
                protocol.ERR_NO_CONNECTION,
                f"send to instance {instance_id} failed: {exc}",
            ) from exc
        try:
            resp = await asyncio.wait_for(fut, cmd_timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            if audit_id is not None:
                await _safe_update_outcome(db, audit_id, "error", "timeout")
            raise CommandError(protocol.ERR_TIMEOUT, "command timed out")
    finally:
        # Always drop the pending entry — on success, timeout, or a broken socket.
        conn_state.pending_commands.pop(request_id, None)

    if not resp.get("ok"):
        error = resp.get("error") or {}
        code = error.get("code") or protocol.ERR_INTERNAL
        if audit_id is not None:
            outcome = "disabled" if code == protocol.ERR_JS_DISABLED else "error"
            await _safe_update_outcome(db, audit_id, outcome, code)
        raise CommandError(code, error.get("message"))

    result = resp.get("result") or {}
    if audit_id is not None:
        await _safe_update_outcome(db, audit_id, "ok", None)
    return result
