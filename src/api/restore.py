"""``POST /api/actions/:id/restore`` — reopen a taken tab (§10).

The intricate endpoint of Фаза 4. Structure (per the brief / §10):

1. Auth (Bearer ``EXT_TOKEN``) then ``require_operational`` (503 if degraded).
2. Load the original action; refuse if it has no source instance/url.
3. Freshness (§6 out-of-pass): the source instance must be ``connected=1``, its
   ``session_id`` unchanged, and ``snapshot_at`` fresher than ``STATE_FRESH_MS`` —
   else request a snapshot and re-check, and if it still cannot be made fresh
   ERROR explicitly. NEVER silently substitute ``main`` (that puts the tab in the
   wrong place).
4. Dedup by URL from the fresh mirror BEFORE opening — restore is idempotent.
5. Open the tab (async ``open_tab`` command, OUTSIDE any transaction).
6. ONE atomic ``db.write`` records: an ``exemptions`` row on the source (so the
   next pass does not evict the restored tab again), the ``restore`` action row,
   ``restored_at`` on the original, and — if this is an UNFINISHED relocation —
   marks the ``relocate`` row ``abandoned`` and writes exemptions on BOTH sides.

Session guard (§5/§10): restore only ever OPENS by URL and never closes a copy by
``tab_id`` (a ``tab_id`` from a dead session addresses a foreign tab), so the guard
is satisfied structurally.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_ext_token, require_not_paused, require_operational
from src.db.actions import (
    insert_action,
    mark_action_abandoned,
    normalize_url,
    set_restored_at,
)
from src.ext import protocol
from src.ext.commands import send_command


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_snapshot_request_id() -> str:
    import uuid

    return f"req-{uuid.uuid4()}"


# --- read helpers (short-lived reader connection) ---------------------------
def _read_action(conn: sqlite3.Connection, action_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM actions WHERE id = ?", (action_id,)
    ).fetchone()


def _read_instance_freshness(conn: sqlite3.Connection, instance_id: str):
    return conn.execute(
        "SELECT connected, session_id, snapshot_at FROM instances WHERE id = ?",
        (instance_id,),
    ).fetchone()


def _mirror_has_url(conn: sqlite3.Connection, instance_id: str, url_norm: str | None) -> bool:
    """True if a live tab in ``instance_id`` has the same normalized URL.

    Dedup is by NORMALIZED url (query/fragment stripped): the archived ``url`` may
    differ only by a tracking parameter and still be "the same tab".
    """
    if url_norm is None:
        return False
    rows = conn.execute(
        "SELECT url FROM tabs WHERE instance_id = ?", (instance_id,)
    ).fetchall()
    return any(normalize_url(r[0]) == url_norm for r in rows)


# --- freshness (§6 out-of-pass) ---------------------------------------------
async def _is_fresh(db, instance_id: str, conn_state, settings) -> bool:
    row = await db.read(lambda c: _read_instance_freshness(c, instance_id))
    if row is None:
        return False
    connected, session_id, snapshot_at = row[0], row[1], row[2]
    if not connected or snapshot_at is None:
        return False
    # The live socket's session must match what the mirror was taken under (§5).
    if session_id != conn_state.session_id:
        return False
    return (_now_ms() - snapshot_at) < settings.state_fresh_ms


async def _request_snapshot(conn_state) -> None:
    request_id = _new_snapshot_request_id()
    conn_state.pending_snapshot_id = request_id
    conn_state.pending_sent_at = _now_ms()
    await conn_state.ws.send_json(
        {"type": protocol.TYPE_SNAPSHOT_REQUEST, "id": request_id}
    )


async def _ensure_fresh(registry, db, instance_id: str, settings):
    """Return the live ConnState once the source is fresh, else raise 409.

    Never returns a substitute instance — the whole point of the explicit error
    is that a non-fresh source must NOT silently become ``main`` (§10).
    """
    conn_state = registry.get(instance_id)
    if conn_state is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"instance {instance_id!r} is not connected; refusing to restore "
                "(would otherwise open the tab in the wrong instance)"
            ),
        )
    if await _is_fresh(db, instance_id, conn_state, settings):
        return conn_state
    # Connected but stale: ask for a fresh snapshot and poll until it lands.
    # NOTE: this directly drives a snapshot_request; when /api/state (with its
    # per-instance single-flight, §13) lands, route through that shared mechanism
    # instead of overwriting pending_snapshot_id here. Poll at 50ms (a fresh reader
    # connection per tick) — coarse enough to stay off the hot path at this scale.
    await _request_snapshot(conn_state)
    deadline = time.monotonic() + settings.snapshot_timeout_ms / 1000.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        if registry.get(instance_id) is not conn_state:
            break  # socket was superseded/dropped underneath us
        if await _is_fresh(db, instance_id, conn_state, settings):
            return conn_state
    raise HTTPException(
        status_code=409,
        detail=(
            f"instance {instance_id!r} snapshot is not fresh; refusing to restore "
            "(never silently substitute the main instance)"
        ),
    )


# --- the transaction --------------------------------------------------------
def _upsert_exemption(conn: sqlite3.Connection, instance_id: str, url: str, until: int) -> None:
    # PK (instance_id, url) => a repeated restore refreshes the same row instead
    # of growing duplicates ("no duplicate exemption growth").
    conn.execute(
        "INSERT INTO exemptions (instance_id, url, until, reason) "
        "VALUES (?, ?, ?, 'restore') "
        "ON CONFLICT(instance_id, url) DO UPDATE SET "
        "until = excluded.until, reason = excluded.reason",
        (instance_id, url, until),
    )


def _record_restore(
    conn: sqlite3.Connection,
    orig: sqlite3.Row,
    instance_from: str,
    url: str,
    url_norm: str | None,
    new_tab_id,
    session_now,
    now: int,
    until: int,
) -> int:
    """Atomic DB side of restore. Returns the new restore action's id."""
    # (a) exemption on the source we reopened in — else the next pass re-evicts it.
    _upsert_exemption(conn, instance_from, url, until)

    # (b) cancel an UNFINISHED relocation for this url (§10). Target the phase-A
    # `relocate` row PRECISELY by id, never by a `url_norm` scan: §4 keeps no
    # intermediate status, so a *completed* past relocation of the same URL also
    # stays `status='done', restored_at=NULL` forever — a `ORDER BY ts DESC` scan
    # could abandon that wrong row (archive corruption + a bogus target exemption).
    # The exact link is known: restoring a phase-B close carries `origin_action_id`
    # to its phase-A relocate (§4); restoring the relocate row itself is its own id.
    candidate_id = orig["origin_action_id"]
    if candidate_id is None:
        candidate_id = orig["id"]
    reloc = conn.execute(
        "SELECT id, instance_to FROM actions "
        "WHERE id = ? AND kind = 'relocate' AND status = 'done' "
        "AND restored_at IS NULL",
        (candidate_id,),
    ).fetchone()
    if reloc is not None:
        mark_action_abandoned(conn, reloc[0])
        # Stamp restored_at on the cancelled relocate too, so a future pass-undo
        # (which skips rows with restored_at set) never re-touches it even when it
        # is a separate row from `orig` (a phase-B close being restored).
        set_restored_at(conn, reloc[0], now)
        target = reloc[1]
        if target:
            # Exemption on BOTH sides — source (above) AND target (here).
            _upsert_exemption(conn, target, url, until)

    # (c) the restore action row (§10: kind='restore', initiator='user',
    # origin_action_id = the restored row). instance_to = where it was reopened.
    restore_id = insert_action(
        conn,
        ts=now,
        kind="restore",
        status="done",
        initiator="user",
        origin_action_id=orig["id"],
        instance_from=instance_from,
        instance_to=instance_from,
        session_id_from=orig["session_id_from"],
        tab_id_to=new_tab_id,
        session_id_to=session_now,
        url=url,
        url_norm=url_norm,
        title=orig["title"],
        pinned=orig["pinned"],
        detail="restore",
    )

    # (d) mark the original restored (idempotency marker; undo skips these).
    set_restored_at(conn, orig["id"], now)
    return restore_id


def _open_tab_params(orig: sqlite3.Row, url: str) -> dict:
    now = _now_ms()
    src_last = orig["src_last_active_at"]
    src_opened = orig["src_opened_at"]
    return {
        "url": url,
        "pinned": bool(orig["pinned"]) if orig["pinned"] is not None else False,
        "active": False,
        # Seed the copy's clocks from the source so a restored tab is not treated
        # as brand-new by the next pass (§7 clock inheritance).
        "seed_age_ms": (now - src_last) if src_last is not None else None,
        "seed_opened_ago_ms": (now - src_opened) if src_opened is not None else None,
        "seed_age_unknown": bool(orig["src_age_unknown"]),
    }


# --- the reusable core (restore ONE archived row) ---------------------------
async def restore_row(app, orig, *, initiator: str = "user", write_on_present: bool = False) -> dict:
    """Reopen the source tab recorded by one archived ``actions`` row (§10).

    THE restore mechanism, factored out of the endpoint so pass-undo reuses it per
    row instead of reimplementing it. Freshens the source (§6, never a silent
    ``main``), dedups by URL, opens the tab OUTSIDE any transaction, then records the
    exemption + restore row + (for an unfinished relocation) the ``relocate``
    cancellation in ONE atomic ``db.write`` (all via :func:`_record_restore`).

    ``write_on_present`` — when the URL is ALREADY live in the source (dedup hit):
    the endpoint returns without writing (idempotent restore), but pass-undo passes
    ``True`` so the exemptions + ``restored_at`` markers are STILL written — otherwise
    undoing a relocation whose source was never closed would leave no exemption and
    the next pass would re-evict it ("undo looks broken", §10).

    Returns ``{restored, reason, action_id, tab_id}``. Raises ``HTTPException`` 422
    (no source/url) or 409 (source not freshenable) — the caller decides whether to
    propagate (endpoint) or isolate per row (undo).
    """
    db = app.state.db
    registry = app.state.ext_registry
    settings = app.state.settings

    # NOTE: restore does NOT short-circuit on restored_at. Idempotency is provided by
    # the dedup-by-URL check below against the FRESH mirror (§10 "Перед открытием —
    # дедуп по URL"). (restored_at remains the marker UNDO uses to skip rows.)
    instance_from = orig["instance_from"]
    url = orig["url"]
    if not instance_from or not url:
        raise HTTPException(
            status_code=422, detail="action has no source instance or url to restore"
        )
    url_norm = orig["url_norm"] or normalize_url(url)

    # Freshness (§6 out-of-pass) — explicit error, never a silent `main`.
    conn_state = await _ensure_fresh(registry, db, instance_from, settings)
    session_now = conn_state.session_id

    # Dedup by URL from the FRESH mirror, BEFORE opening — restore is idempotent.
    present = await db.read(lambda c: _mirror_has_url(c, instance_from, url_norm))
    new_tab_id = None
    if not present:
        # Open the tab (async command, OUTSIDE any transaction).
        result = await send_command(
            registry,
            db,
            instance_from,
            protocol.CMD_OPEN_TAB,
            _open_tab_params(orig, url),
            cmd_timeout_ms=settings.cmd_timeout_ms,
            initiator=initiator,
        )
        new_tab_id = result.get("tabId")
    elif not write_on_present:
        # Endpoint path: already present => nothing to open and nothing to record.
        return {"restored": False, "reason": "already_present", "action_id": None, "tab_id": None}

    now = _now_ms()
    until = now + settings.restore_exemption_min * 60_000
    restore_id = await db.write(
        lambda c: _record_restore(
            c, orig, instance_from, url, url_norm, new_tab_id, session_now, now, until
        )
    )
    return {
        "restored": not present,
        "reason": "already_present" if present else None,
        "action_id": restore_id,
        "tab_id": new_tab_id,
    }


# --- the endpoint -----------------------------------------------------------
async def restore_action(request: Request) -> JSONResponse:
    require_ext_token(request)      # 401 before anything else
    require_operational(request)    # 503 in degraded mode
    await require_not_paused(request)  # 423 while paused (§7 gate)

    action_id = request.path_params["action_id"]
    orig = await request.app.state.db.read(lambda c: _read_action(c, action_id))
    if orig is None:
        raise HTTPException(status_code=404, detail=f"action {action_id} not found")

    res = await restore_row(request.app, orig)
    if res["restored"]:
        return JSONResponse(
            {"ok": True, "restored": True, "action_id": res["action_id"], "tab_id": res["tab_id"]}
        )
    return JSONResponse({"ok": True, "restored": False, "reason": res["reason"]})
