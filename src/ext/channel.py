"""The WSS ``/ext`` endpoint: accept, hello, snapshot, heartbeat, disconnect.

Structure of one connection:

1. accept, then wait for a ``hello``; validate + (under the registry lock) decide
   evict / reject / register; on success run the epoch-bumping upsert, reply
   ``hello_ack{ok:true}`` and send the first ``snapshot_request``.
2. start the heartbeat task and enter the receive loop, dispatching ``pong`` and
   ``snapshot`` by ``type`` (a reply is correlated by its ``id``).
3. the finalizer ALWAYS runs the epoch-guarded disconnect UPDATE for the epoch
   THIS socket owned, cancels the heartbeat, and de-registers itself by identity.

All websocket I/O (send/recv/close) is async and lives OUTSIDE the DB
transaction; every DB mutation is one sync ``fn(conn)`` via ``Database.write``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.db import queries
from src.ext import protocol
from src.ext.registry import ConnState, Registry
from src.ext.snapshot import apply_snapshot


def _now_ms() -> int:
    return int(time.time() * 1000)


def _count_rejection(app) -> None:
    # In-process counter; a later phase's /metrics exports it
    # (curator_auth_rejections_total). Kept on app.state, no endpoint here yet.
    app.state.ext_rejections = getattr(app.state, "ext_rejections", 0) + 1


async def ext_channel(websocket: WebSocket) -> None:
    app = websocket.app
    settings = app.state.settings

    # Degraded DB refuses the channel (§12): a migration failure means the schema
    # cannot be trusted for authoritative snapshots. Accept then close so the
    # client sees a clean 1011 rather than a handshake-level rejection.
    await websocket.accept()
    if getattr(app.state, "degraded", False):
        await websocket.close(code=1011)
        return

    # The first frame must be a hello. Anything else, or a transport drop, ends it.
    try:
        first = await websocket.receive_json()
    except (WebSocketDisconnect, KeyError, ValueError):
        # A malformed/absent opening frame ends the connection. Close explicitly
        # (like every other early exit) instead of leaving it to the ASGI server.
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - peer may already be gone
            pass
        return

    conn_state = await _handle_hello(websocket, app, settings, first)
    if conn_state is None:
        return  # hello was rejected and the socket already closed.

    instance_id = first["instanceId"]
    registry: Registry = app.state.ext_registry
    db = app.state.db

    conn_state.heartbeat_task = asyncio.create_task(
        _heartbeat(websocket, conn_state, settings.heartbeat_ms)
    )

    try:
        await _receive_loop(websocket, app, conn_state, instance_id)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a broken socket must still finalize cleanly
        logger.exception("ext channel error for instance {}", instance_id)
    finally:
        await _finalize(websocket, db, registry, conn_state, instance_id)


async def _handle_hello(
    websocket: WebSocket, app, settings, msg: dict[str, Any]
) -> ConnState | None:
    """Validate + register a hello. Returns the ConnState on success, else None
    (having sent a failing ``hello_ack`` and closed the socket)."""
    if not isinstance(msg, dict) or msg.get("type") != protocol.TYPE_HELLO:
        # Protocol violation: the opening frame was not a hello.
        await _reject(websocket, app, None, protocol.REJECT_PROTOCOL)
        return None

    allowed = protocol.parse_origins(settings.ext_allowed_origins)
    if not allowed and not getattr(app.state, "warned_open_origin", False):
        app.state.warned_open_origin = True
        logger.warning(
            "EXT_ALLOWED_ORIGINS is empty: /ext accepts any origin (open) — "
            "tighten this once the extension id is known (§12)"
        )

    reason = protocol.hello_reject_reason(
        msg, settings.protocol_version, settings.ext_token, allowed
    )
    if reason is not None:
        # A blank/missing instanceId cannot key an instances row, so it is only
        # counted + closed; every other reason is also recorded in the DB.
        instance_id = msg.get("instanceId")
        record = reason != protocol.REJECT_INSTANCE
        await _reject(
            websocket,
            app,
            instance_id if record and isinstance(instance_id, str) else None,
            reason,
        )
        return None

    instance_id = msg["instanceId"]
    install_uuid = msg.get("installUuid") or ""
    registry: Registry = app.state.ext_registry
    db = app.state.db

    # Critical section: decide evict-vs-reject and swap the entry atomically so a
    # second hello cannot interleave across the await old.close() (§6).
    async with registry.lock:
        existing = registry.get(instance_id)
        if existing is not None:
            if existing.install_uuid == install_uuid:
                # Legitimate reconnect: evict the old socket (guarded disconnect
                # UPDATE for ITS epoch + close), then take over below.
                if existing.heartbeat_task is not None:
                    existing.heartbeat_task.cancel()
                await db.write(
                    lambda c: queries.mark_disconnected(
                        c, instance_id, existing.conn_epoch
                    )
                )
                try:
                    await existing.ws.close(code=1012)  # service restart / takeover
                except Exception:  # noqa: BLE001
                    pass
                registry.remove_if_current(instance_id, existing)
            else:
                # Different installUuid => a copied instance.json => duplicate.
                # Reject THIS new socket; the existing one stays alive & untouched.
                await _reject(
                    websocket, app, instance_id, protocol.REJECT_DUPLICATE
                )
                return None

        now = _now_ms()
        session_id = msg.get("sessionId")
        title = msg.get("title")
        allow_execute_js = bool(msg.get("allowExecuteJs"))
        new_epoch = await db.write(
            lambda c: queries.hello_upsert(
                c, instance_id, session_id, title, allow_execute_js, now
            )
        )
        conn_state = ConnState(
            ws=websocket,
            conn_epoch=new_epoch,
            install_uuid=install_uuid,
            session_id=session_id,
        )
        registry.put(instance_id, conn_state)

    await websocket.send_json(
        {
            "type": protocol.TYPE_HELLO_ACK,
            "ok": True,
            "instanceId": instance_id,
            "connEpoch": new_epoch,
        }
    )
    # Immediately request the first snapshot; record its server send-time.
    await _send_snapshot_request(websocket, conn_state)
    return conn_state


async def _send_snapshot_request(websocket: WebSocket, conn_state: ConnState) -> None:
    request_id = _new_request_id()
    conn_state.pending_snapshot_id = request_id
    conn_state.pending_sent_at = _now_ms()
    await websocket.send_json(
        {"type": protocol.TYPE_SNAPSHOT_REQUEST, "id": request_id}
    )


_request_seq = 0


def _new_request_id() -> str:
    global _request_seq
    _request_seq += 1
    return f"req-{_request_seq}"


async def _reject(websocket: WebSocket, app, instance_id: str | None, reason: str) -> None:
    """Record (when we have an id), count, send a failing hello_ack, and close."""
    _count_rejection(app)
    if instance_id:
        db = app.state.db
        await db.write(
            lambda c: queries.record_rejection(c, instance_id, reason, _now_ms())
        )
    try:
        await websocket.send_json(
            {"type": protocol.TYPE_HELLO_ACK, "ok": False, "error": {"code": reason}}
        )
        await websocket.close()
    except Exception:  # noqa: BLE001 - the peer may already be gone
        pass


async def _receive_loop(
    websocket: WebSocket, app, conn_state: ConnState, instance_id: str
) -> None:
    db = app.state.db
    registry: Registry = app.state.ext_registry
    while True:
        msg = await websocket.receive_json()
        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == protocol.TYPE_PONG:
            conn_state.alive = True
        elif mtype == protocol.TYPE_SNAPSHOT:
            await _handle_snapshot(db, registry, conn_state, instance_id, msg)
        # Any other frame is unsolicited and ignored (§6: pong is the only
        # unsolicited client message; everything else is a reply keyed by id).


async def _handle_snapshot(
    db, registry: Registry, conn_state: ConnState, instance_id: str, msg: dict[str, Any]
) -> None:
    # Ignore a reply whose id is unknown/stale, or that arrived on a socket whose
    # epoch is no longer current (§6). Reading back the registry entry proves this
    # socket still owns the instance.
    if msg.get("id") != conn_state.pending_snapshot_id:
        return
    if registry.get(instance_id) is not conn_state:
        return
    sent_at = conn_state.pending_sent_at
    conn_state.pending_snapshot_id = None
    conn_state.pending_sent_at = None
    now = _now_ms()
    await db.write(
        lambda c: apply_snapshot(
            c, instance_id, msg, sent_at, now, conn_state.conn_epoch
        )
    )
    # Keep the connection's notion of the live session in sync with what it just
    # reported (used to reject stale replies / future readiness checks).
    conn_state.session_id = msg.get("sessionId")


async def _heartbeat(websocket: WebSocket, conn_state: ConnState, heartbeat_ms: int) -> None:
    """Send a ping every interval; close after two consecutive misses (§6).

    Starlette gives no forced drop — the mechanism is close()+cancel. The miss
    decision is the pure ``protocol.heartbeat_step`` so it is testable without a
    clock; this coroutine only supplies the timing and the I/O.
    """
    interval = heartbeat_ms / 1000.0
    misses = 0
    try:
        while True:
            await asyncio.sleep(interval)
            misses, disconnect = protocol.heartbeat_step(conn_state.alive, misses)
            if disconnect:
                try:
                    await websocket.close(code=1011)
                except Exception:  # noqa: BLE001
                    pass
                return
            conn_state.alive = False
            await websocket.send_json({"type": protocol.TYPE_PING})
    except (WebSocketDisconnect, RuntimeError):
        # Socket went away underneath us; the receive-loop finalizer handles cleanup.
        return
    except asyncio.CancelledError:
        raise


async def _finalize(
    websocket: WebSocket,
    db,
    registry: Registry,
    conn_state: ConnState,
    instance_id: str,
) -> None:
    """Guaranteed cleanup for a connection. Idempotent and epoch-guarded."""
    if conn_state.heartbeat_task is not None:
        conn_state.heartbeat_task.cancel()
    # Epoch-guarded disconnect write for the epoch THIS socket owned. If a newer
    # connection has since taken over, the epoch no longer matches and this is a
    # no-op — the whole point of the guard.
    try:
        await db.write(
            lambda c: queries.mark_disconnected(c, instance_id, conn_state.conn_epoch)
        )
    except Exception:  # noqa: BLE001
        logger.exception("disconnect finalize write failed for {}", instance_id)
    async with registry.lock:
        registry.remove_if_current(instance_id, conn_state)
    try:
        await websocket.close()
    except Exception:  # noqa: BLE001
        pass
