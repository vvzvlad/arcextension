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
import secrets
import time
from typing import Any

from loguru import logger
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.api.auth_metrics import auth_rejections
from src.curator.enroll import read_enroll_window
from src.db import queries
from src.ext import protocol
from src.ext.commands import resolve_response
from src.ext.registry import ConnState, Registry
from src.ext.snapshot import apply_snapshot

# The first frame of an accepted connection must arrive within this many seconds; an
# opened-but-silent socket (a stalled or hostile peer) must not tie up a pre-auth slot
# forever (§2). Kept small — a real client sends its hello/enroll immediately.
_FIRST_FRAME_TIMEOUT_S = 10

# --- the SILENT-socket quota (§2) --------------------------------------------
# The pre-auth ceiling alone cannot protect a reconnect. It is checked BEFORE accept(), so
# at that moment nothing distinguishes a hello from an already-approved instance, an
# enroll_request, and a socket that will say nothing for the full _FIRST_FRAME_TIMEOUT_S.
# One shared ceiling therefore lets a flood of silent sockets hold every slot: at
# ENROLL_PREAUTH_MAX=128 and a 10 s timeout, ~13 connections per second keep the whole
# fleet's RECONNECTS refused indefinitely — every instance falls out of curation while the
# alerts read "absent", and it costs the attacker no credential at all.
#
# A second, SMALLER hard ceiling on silent sockets does NOT fix this, and it is worth
# writing down why, because it is the obvious move: since a reconnect is also silent at
# handshake time (it has to be accepted before it can send its hello), the smaller ceiling
# would refuse reconnects too — at a QUARTER of the connection rate. It makes the same
# attack four times cheaper.
#
# What actually separates the two is TIME, which is the one thing a flood cannot fake: a
# real client sends its first frame within a round-trip of the handshake, while a flood's
# whole leverage is holding the slot for the full timeout. So the quota governs the
# TIMEOUT rather than admission: once at or above a quarter of the ceiling is occupied by
# sockets that have not spoken, newly accepted sockets get one second instead of ten to
# say something. Nobody is refused, a speaker is never affected, and the connection rate
# an attacker needs to hold the ceiling rises by the same factor as the timeout shrinks
# (~13/s -> ~128/s at the defaults). The cost is that a client whose first frame takes
# more than a second to arrive is dropped WHILE the service is under this exact pressure;
# it reconnects on its next alarm, which is the right trade against losing the fleet.
# Derived from ENROLL_PREAUTH_MAX rather than being a fifth ENV knob: the two numbers are
# a ratio, not independent policy.
_SILENT_SHARE = 4
_FIRST_FRAME_TIMEOUT_UNDER_PRESSURE_S = 1


def silent_preauth_max(preauth_max: int) -> int:
    """How many not-yet-identified sockets are tolerated before the first-frame timeout is
    compressed (§2). A quarter of the pre-auth ceiling, floored at 1."""
    return max(1, int(preauth_max) // _SILENT_SHARE)


def first_frame_timeout_s(silent_count: int, preauth_max: int) -> float:
    """Seconds this socket gets to send its first frame: the normal
    :data:`_FIRST_FRAME_TIMEOUT_S`, or the compressed
    :data:`_FIRST_FRAME_TIMEOUT_UNDER_PRESSURE_S` once the silent-socket quota is reached.

    The threshold is NON-strict (``>=``): the quota is the count at which the compressed
    timeout starts applying, not a number that must be exceeded.

    ``silent_count`` counts the OTHER sockets that are currently waiting to be identified
    (this one's own reservation excluded), so a single connection to an idle service always
    gets the full window.
    """
    if silent_count >= silent_preauth_max(preauth_max):
        return _FIRST_FRAME_TIMEOUT_UNDER_PRESSURE_S
    return _FIRST_FRAME_TIMEOUT_S

# Client-proposed strings recorded verbatim from an enroll_request are length-clamped
# before they touch the DB (§36): a hostile peer must not stash megabytes in the
# operator-facing pending list.
_MAX_SUGGESTED_TITLE = 200
_MAX_ORIGIN = 300
# install_uuid (the enroll_requests PRIMARY KEY) and the raw secret (which the server
# hashes into the NOT-NULL secret_hash credential) are the LARGEST unbounded fields a
# hostile peer with a valid window code could stash in the operator-facing pending list
# (§36), so they get their own ceilings. Unlike title/origin they are NOT truncated —
# truncating a key or a credential silently corrupts identity — an overlength value is a
# malformed frame (REJECT_PROTOCOL). A real raw secret is 64 hex chars (32 bytes), a real
# UUID 36; the cap stays generous.
_MAX_INSTALL_UUID = 200
_MAX_SECRET = 128
# The same ceilings on the HELLO path. A hello is authenticated, but "authenticated" only
# means the peer holds an approved secret — it does not make the rest of the frame trusted,
# and every value below lands in the DB and then in an operator-facing surface (§36):
#   * ``title`` is display text, so it is CLAMPED exactly like ``suggested_title`` — an
#     approved instance must not be able to replace its carefully-clamped enrolled name
#     with a multi-megabyte string that then travels into /admin, /api/state and the
#     console. A non-str title becomes NULL instead of reaching sqlite3 as a list/dict,
#     which raised an unhandled InterfaceError inside a write whose only guard is a
#     `finally`.
#   * ``sessionId`` is IDENTITY, not display text: the mirror compares it verbatim against
#     the value later reported in snapshots, and a relocation stays live only while both
#     endpoints' sessions still match. Truncating it would silently corrupt those
#     comparisons, so an oversized/non-str session id is a malformed frame
#     (REJECT_PROTOCOL) — the same rule install_uuid / secret already follow.
_MAX_TITLE = 200
_MAX_SESSION_ID = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clamp(value: Any, limit: int) -> str | None:
    """Coerce a client-supplied string to at most ``limit`` chars; non-str -> None."""
    if not isinstance(value, str):
        return None
    return value[:limit]


def _count_rejection(app) -> None:
    # In-process counter; a later phase's /metrics exports it
    # (curator_auth_rejections_total). Kept on app.state, no endpoint here yet.
    app.state.ext_rejections = getattr(app.state, "ext_rejections", 0) + 1


async def ext_channel(websocket: WebSocket) -> None:
    app = websocket.app
    settings = app.state.settings

    # --- Pre-auth connection ceiling, BEFORE accept() (§2) -------------------
    # A flood of sockets that are accepted but never say hello/enroll would tie up
    # memory and (worse) real TLS sessions. Refuse the Nth pre-auth socket at the
    # handshake WITHOUT accept(): closing a not-yet-accepted socket sends a bare
    # handshake rejection that costs no TLS session. The counter tracks sockets that
    # have RESERVED a pre-auth slot (reserved just below, before accept()) and not yet
    # completed a hello/enroll; a successful hello releases its slot before entering the
    # long-lived receive loop, so live authenticated connections are NOT counted against
    # the pre-auth ceiling.
    #
    # The SILENT count is read here too, but it never refuses anybody — it decides how long
    # this socket gets to identify itself (see ``first_frame_timeout_s``: a second hard
    # ceiling would refuse reconnects at a quarter of the attack rate).
    preauth = getattr(app.state, "ext_preauth_count", 0)
    silent = getattr(app.state, "ext_silent_count", 0)
    first_frame_timeout = first_frame_timeout_s(silent, settings.enroll_preauth_max)
    if preauth >= settings.enroll_preauth_max:
        auth_rejections.incr("capacity")
        _count_rejection(app)
        try:
            await websocket.close()  # pre-accept: a handshake rejection, no TLS session
        except Exception:  # noqa: BLE001 - peer may already be gone
            pass
        return
    # Reserve both slots NOW (no await between the reads above and these writes, so the
    # reservation is atomic on the single-threaded event loop). Each ``released`` flag
    # guards its own release so a slot is given back exactly once, whether this becomes a
    # hello, an enroll, a timeout, or an early close.
    app.state.ext_preauth_count = preauth + 1
    app.state.ext_silent_count = silent + 1
    released = False
    silent_released = False

    def _release_preauth() -> None:
        nonlocal released
        if not released:
            released = True
            app.state.ext_preauth_count = getattr(app.state, "ext_preauth_count", 1) - 1

    def _release_silent() -> None:
        nonlocal silent_released
        if not silent_released:
            silent_released = True
            app.state.ext_silent_count = getattr(app.state, "ext_silent_count", 1) - 1

    try:
        # Degraded DB refuses the channel (§12): a migration failure means the schema
        # cannot be trusted for authoritative snapshots. Accept then close so the
        # client sees a clean 1011 rather than a handshake-level rejection.
        await websocket.accept()
        if getattr(app.state, "degraded", False):
            await websocket.close(code=1011)
            return

        # The first frame must arrive within a bounded time (§2 — previously
        # unbounded), and that bound is compressed while silent sockets are over quota
        # (see first_frame_timeout_s). A timeout, malformed frame, or transport drop ends
        # the socket.
        try:
            first = await asyncio.wait_for(
                websocket.receive_json(), timeout=first_frame_timeout
            )
        except (asyncio.TimeoutError, WebSocketDisconnect, KeyError, ValueError):
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001 - peer may already be gone
                pass
            return
        finally:
            # Identified (or gone): give the silent quota back either way, so the small
            # quota only ever measures sockets that are CURRENTLY saying nothing.
            _release_silent()

        mtype = first.get("type") if isinstance(first, dict) else None
        if mtype == protocol.TYPE_ENROLL_REQUEST:
            # A not-yet-approved client. Record (or refuse) the request and close; the
            # socket is NOT kept — approval is async ("удерживать сокет не нужно").
            await _handle_enroll(websocket, app, settings, first)
            return
        if mtype != protocol.TYPE_HELLO:
            # The opening frame was neither an enroll_request nor a hello.
            await _reject(websocket, app, None, protocol.REJECT_PROTOCOL)
            return

        result = await _handle_hello(websocket, app, settings, first)
        if result is None:
            return  # hello was rejected and the socket already closed.
        conn_state, instance_id = result
    finally:
        # A hello/enroll/timeout/close has been decided; release the pre-auth slot.
        # (On a SUCCESSFUL hello this runs as we leave the block, before the long
        # receive loop below, so a live connection never occupies a pre-auth slot.)
        _release_preauth()
        # Belt-and-braces: the silent slot is normally handed back the moment the first
        # frame is decided, but an early return (degraded close, accept() failure) must
        # not leak it either.
        _release_silent()

    registry: Registry = app.state.ext_registry
    db = app.state.db

    try:
        # hello_ack + the first snapshot_request are sent INSIDE this try/finally:
        # they run AFTER _handle_hello committed connected=1 and registered the
        # socket, so a peer drop here MUST still reach _finalize (epoch-guarded
        # mark_disconnected + de-register). Otherwise a phantom connected=1 row and
        # an orphan ConnState would linger with no live socket (§6 — the invariant
        # the whole epoch guard exists to protect).
        await websocket.send_json(
            {
                "type": protocol.TYPE_HELLO_ACK,
                "ok": True,
                "instanceId": instance_id,
                "connEpoch": conn_state.conn_epoch,
            }
        )
        await _send_snapshot_request(websocket, conn_state)
        conn_state.heartbeat_task = asyncio.create_task(
            _heartbeat(websocket, conn_state, settings.heartbeat_ms)
        )
        await _receive_loop(websocket, app, conn_state, instance_id)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a broken socket must still finalize cleanly
        logger.exception("ext channel error for instance {}", instance_id)
    finally:
        await _finalize(websocket, db, registry, conn_state, instance_id)


async def _handle_enroll(
    websocket: WebSocket, app, settings, msg: dict[str, Any]
) -> None:
    """Gate + record a not-yet-approved client's enroll_request (§2).

    Checks protocol/window/code via the pure :func:`protocol.enroll_reject_reason` and —
    only if all gates pass — writes ONE ``enroll_requests`` row and replies
    ``enroll_pending``. On any gate failure it sends ``enroll_rejected{reason}`` and writes
    NO row (issue acceptance 2/3). The socket is always closed afterwards: approval is
    asynchronous, so the socket is never kept.

    **DB reads are rationed against the credential check.** This handler runs on a
    completely UNAUTHENTICATED socket, and every ``Database.read`` opens a fresh sqlite
    connection on a thread from the shared pool — so each read a hostile peer can trigger
    before its code is checked is a lever on the whole service (the curator pass,
    ``/api/state`` and ``/metrics`` all queue behind the same pool). Hence:

    * the ``protocolVersion`` gate — the one check that needs no DB at all — runs FIRST,
      so a wrong-version flood costs zero connections;
    * the window/code check needs exactly ONE read, and there is no second one: the
      capacity pre-count was pure duplication, because
      :func:`queries.upsert_enroll_request_capped` re-counts inside the write transaction
      and is the authoritative gate anyway (a pre-read in its own transaction could never
      be more than advisory).
    """
    db = app.state.db
    now = _now_ms()
    # Cheapest gate first, with NO DB touch. The pure helper below re-checks it and owns the
    # CONFIG-shaped head of the order (§2: protocol → window → code); this only
    # short-circuits its DB-free first step so a wrong-version peer never reaches the pool.
    # The full order of record continues HERE, and only here: after the helper comes the
    # structural check on installUuid/secret, and then capacity + the frozen-secret
    # conflict, both decided inside the write transaction below rather than ahead of it.
    if msg.get("protocolVersion") != settings.protocol_version:
        await _reject_enroll(websocket, app, protocol.REJECT_PROTOCOL)
        return

    window = await db.read(lambda c: read_enroll_window(c, now=now))
    window_open = window.open
    # A missing/blank/mismatched code => code_ok False (acceptance 2: an enroll_request
    # WITHOUT a code at an open window must write NO row). The window must also be open
    # for a code to be valid at all. Compared in CONSTANT TIME: the window code is a short
    # secret typed by the operator, and a plain `==` leaks its prefix through timing to the
    # very unauthenticated peer this gate exists to stop. Both sides are encoded (errors
    # ignored) because compare_digest raises TypeError on a non-ASCII str, which a hostile
    # peer can trivially send.
    code = msg.get("code")
    code_ok = (
        window_open
        and isinstance(code, str)
        and bool(code.strip())
        and isinstance(window.code, str)
        and secrets.compare_digest(
            code.encode("utf-8", "ignore"), window.code.encode("utf-8", "ignore")
        )
    )
    # Capacity is decided authoritatively inside the write below, so nothing to pre-read —
    # and the helper no longer takes a capacity flag at all (it used to, and the only
    # caller pinned it to True, which made the branch dead and the docstring wrong).
    reason = protocol.enroll_reject_reason(
        msg, settings.protocol_version, window_open, code_ok
    )
    if reason is None:
        # Structural check AFTER the code gate (so a wrong code never reveals whether the
        # frame was well-formed, and no row is written for a bad code): a row needs a
        # non-blank install_uuid (its PRIMARY KEY) and a non-blank raw secret (which we
        # hash into the NOT-NULL secret_hash below).
        install_uuid = msg.get("installUuid")
        raw_secret = msg.get("secret")
        if (
            not isinstance(install_uuid, str)
            or not install_uuid.strip()
            or len(install_uuid) > _MAX_INSTALL_UUID
            or not isinstance(raw_secret, str)
            or not raw_secret.strip()
            or len(raw_secret) > _MAX_SECRET
        ):
            # Missing/blank OR overlength (§36: no megabytes in the operator-facing list).
            reason = protocol.REJECT_PROTOCOL

    if reason is not None:
        await _reject_enroll(websocket, app, reason)
        return

    origin = _clamp(msg.get("origin"), _MAX_ORIGIN)
    suggested_title = _clamp(msg.get("title"), _MAX_SUGGESTED_TITLE)
    # Hash the raw secret on receipt: only the sha256 is ever stored (option A). The
    # /admin approve later copies this same secret_hash onto the instance row.
    secret_hash = queries.sha256_hex(raw_secret)
    # THE capacity gate (count + write under one transaction, so racing enrolls cannot
    # overshoot enroll_max_pending) and the frozen-credential gate, both authoritative.
    outcome = await db.write(
        lambda c: queries.upsert_enroll_request_capped(
            c,
            install_uuid,
            origin,
            suggested_title,
            settings.protocol_version,
            secret_hash,
            now,
            settings.enroll_max_pending,
            settings.enroll_request_ttl_min * 60_000,
        )
    )
    if outcome == queries.ENROLL_AT_CAPACITY:
        await _reject_enroll(websocket, app, protocol.ENROLL_CAPACITY)
        return
    if outcome == queries.ENROLL_SECRET_MISMATCH:
        # A pending request for this install_uuid already carries a DIFFERENT secret; the
        # stored credential is frozen at creation (queries._UPSERT_ENROLL_REQUEST), so this
        # one is refused rather than silently queued behind a credential the client does
        # not hold. Counted under its own reason so a burst — the shape a substitution
        # attempt makes — is visible in /metrics.
        await _reject_enroll(websocket, app, protocol.ENROLL_SECRET_CONFLICT)
        return
    try:
        await websocket.send_json({"type": protocol.TYPE_ENROLL_PENDING})
        await websocket.close()
    except Exception:  # noqa: BLE001 - peer may already be gone
        pass


async def _reject_enroll(websocket: WebSocket, app, reason: str) -> None:
    """Count + send ``enroll_rejected{reason}`` + close. Writes NO row, by construction.

    The enroll twin of :func:`_reject` (which answers on the hello path and can record the
    reason on a known instances row); an enroll_request has no row to record against.
    """
    # Stable metric labels: issue §37 alerts on exactly ``enroll_bad_code``.
    auth_rejections.incr("enroll_" + reason)
    _count_rejection(app)
    try:
        await websocket.send_json(
            {"type": protocol.TYPE_ENROLL_REJECTED, "reason": reason}
        )
        await websocket.close()
    except Exception:  # noqa: BLE001 - peer may already be gone
        pass


async def _handle_hello(
    websocket: WebSocket, app, settings, msg: dict[str, Any]
) -> tuple[ConnState, str] | None:
    """Validate + register a secret-based hello (§2).

    Returns ``(ConnState, resolved_instance_id)`` on success, else ``None`` (having sent
    a failing ``hello_ack`` and closed the socket). Authentication is by the per-install
    SECRET: the client sends the RAW ``secret`` over TLS; the channel hashes it server-side
    (:func:`queries.resolve_secret`) and matches the stored sha256, taking the
    SERVER-assigned id from that row — the client no longer self-reports a trusted
    instanceId, and the DB stores only the sha256, so a DB-only leak yields no usable
    credential.
    """
    raw_secret = msg.get("secret")
    if (
        not isinstance(raw_secret, str)
        or not raw_secret.strip()
        or len(raw_secret) > _MAX_SECRET
    ):
        # No usable secret (missing/blank, or oversized — a hostile peer must not make the
        # server hash a multi-MB string; symmetric with the enroll cap) => auth failure.
        # No row to record against (unknown id).
        await _reject(websocket, app, None, protocol.REJECT_AUTH)
        return None

    db = app.state.db
    resolved = await db.read(lambda c: queries.resolve_secret(c, raw_secret))
    if resolved is None:
        # The secret matches no instance at all — never approved, or deleted.
        await _reject(websocket, app, None, protocol.REJECT_UNKNOWN)
        return None
    instance_id, status = resolved
    if status == "revoked":
        # A known-but-revoked instance: record the reject on ITS row (still revoked),
        # tell the client to stop (§7).
        await _reject(websocket, app, instance_id, protocol.REJECT_REVOKED)
        return None
    if status != "active":
        # 'pending' (or any non-active state): approved-not-yet, treat as unknown so the
        # client keeps waiting rather than acting on a revoked verdict.
        await _reject(websocket, app, None, protocol.REJECT_UNKNOWN)
        return None

    reason = protocol.hello_reject_reason(msg, settings.protocol_version, instance_id)
    if reason is not None:
        # protocol: the id is a real active instance, so record the reject.
        await _reject(websocket, app, instance_id, reason)
        return None

    # Field hygiene BEFORE the registry lock, symmetric with the enroll path (which does
    # the same structural checks in this module rather than in the pure validator): an
    # authenticated peer is not a trusted peer, and both values below go straight into the
    # DB and from there to operator-facing surfaces. See _MAX_TITLE / _MAX_SESSION_ID for
    # why one is clamped and the other refused.
    session_id = msg.get("sessionId")
    if session_id is not None and (
        not isinstance(session_id, str) or len(session_id) > _MAX_SESSION_ID
    ):
        await _reject(websocket, app, instance_id, protocol.REJECT_PROTOCOL)
        return None
    title = _clamp(msg.get("title"), _MAX_TITLE)

    install_uuid = msg.get("installUuid") or ""
    registry: Registry = app.state.ext_registry

    # Critical section: decide evict-vs-reject and swap the entry atomically so a
    # second hello cannot interleave across the await old.close() (§6).
    gone = False
    conn_state: ConnState | None = None
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
                # Same secret from a DIFFERENT installUuid => a copied instance.json =>
                # duplicate. Reject THIS new socket; the existing one stays untouched.
                await _reject(
                    websocket, app, instance_id, protocol.REJECT_DUPLICATE
                )
                return None

        now = _now_ms()
        allow_execute_js = bool(msg.get("allowExecuteJs"))
        new_epoch = await db.write(
            lambda c: queries.hello_upsert(
                c, instance_id, session_id, title, allow_execute_js, now
            )
        )
        if new_epoch is None:
            # The active row vanished (deleted/revoked) between resolve_secret and this
            # write. Do NOT int(None); fall out and reject cleanly below (outside the
            # lock) — never register a ConnState for a gone instance.
            gone = True
        else:
            conn_state = ConnState(
                ws=websocket,
                conn_epoch=new_epoch,
                install_uuid=install_uuid,
                session_id=session_id,
            )
            registry.put(instance_id, conn_state)

    if gone or conn_state is None:
        await _reject(websocket, app, None, protocol.REJECT_UNKNOWN)
        return None

    # NOTE: the hello_ack and first snapshot_request are sent by the CALLER, inside
    # its try/finally — so a drop between the commit above and those sends still
    # runs _finalize (no phantom connected=1 / orphan ConnState).
    return conn_state, instance_id


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
    # Also feed the process-memory curator_auth_rejections_total (§12); the reason
    # is the protocol reject code (auth / protocol / origin / duplicate / instance).
    auth_rejections.incr(reason)
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
        elif mtype == protocol.TYPE_RESPONSE:
            # Correlated reply to a service->extension command (§6). Resolve the
            # pending Future by id; an unknown id (late/duplicate) is ignored.
            resolve_response(conn_state, msg)
        # Any other frame is unsolicited and ignored (§6: pong is the only
        # unsolicited client message; everything else is a reply keyed by id).


async def _handle_snapshot(
    db, registry: Registry, conn_state: ConnState, instance_id: str, msg: dict[str, Any]
) -> None:
    # Ignore a reply whose id is unknown/stale, or that arrived on a socket whose
    # epoch is no longer current (§6). Reading back the registry entry proves this
    # socket still owns the instance.
    # A frame with NO id must NOT pass: `msg.get("id")` (None) != pending (also None
    # once consumed) is False, which would apply an UNSOLICITED snapshot with
    # sent_at=None — the sent_at-bounded delete becomes `<= NULL` (never) and a
    # missing sessionId reads as a session change → wipe. Guard `pending is None`.
    pending = conn_state.pending_snapshot_id
    if pending is None or msg.get("id") != pending:
        return
    if registry.get(instance_id) is not conn_state:
        return
    sent_at = conn_state.pending_sent_at
    applied_id = pending
    conn_state.pending_snapshot_id = None
    conn_state.pending_sent_at = None
    now = _now_ms()
    await db.write(
        lambda c: apply_snapshot(
            c, instance_id, msg, sent_at, now, conn_state.conn_epoch
        )
    )
    # Record WHICH request id was just applied so the curator pass can key its
    # readiness on the id it sent (§7), never on the shared ``snapshot_at`` column.
    conn_state.last_applied_snapshot_id = applied_id
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
