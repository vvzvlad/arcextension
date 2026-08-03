"""``POST /api/actions/:id/restore`` — reopen a taken tab (§10).

The intricate endpoint of Фаза 4. Structure (per the brief / §10):

1. Auth (:func:`require_api_caller`, §35 §4) then ``require_operational`` (503 if degraded).
2. Load the original action; refuse if it has no source instance/url.
3. Freshness (§6 out-of-pass): the source instance must be ``connected=1``, its
   ``session_id`` unchanged, and ``snapshot_at`` fresher than ``STATE_FRESH_MS`` —
   else request a snapshot and re-check, and if it still cannot be made fresh
   ERROR explicitly. NEVER silently substitute ``main`` (that puts the tab in the
   wrong place). The request goes through the SHARED :mod:`src.api.freshness`
   mechanism, which never overwrites a pass's ``pending_snapshot_id``.
4. Dedup by URL from the fresh mirror BEFORE opening — restore is idempotent.
5. ``exemptions`` are written BEFORE the open (§10 asks for "one transaction with
   the open"; literally impossible — the open is a WS command outside any
   transaction — so the order is chosen to fail SAFE). A crash between a successful
   ``open_tab`` and the recording write would otherwise leave a reopened tab with no
   exemption, no ``restore`` row and no ``restored_at``: the next pass evicts it
   again. An exemption whose open never happened is harmless — it expires by itself.
6. Open the tab (async ``open_tab`` command, OUTSIDE any transaction).
7. ONE atomic ``db.write`` records: the ``exemptions`` row (refreshed), the
   ``restore`` action row, ``restored_at`` on the original, and — if this is an
   UNFINISHED relocation — marks the ``relocate`` row ``abandoned`` and writes
   exemptions on BOTH sides.

Session guard (§5/§10): restore only ever OPENS by URL and never closes a copy by
``tab_id`` (a ``tab_id`` from a dead session addresses a foreign tab), so the guard
is satisfied structurally.
"""

from __future__ import annotations

import sqlite3

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.freshness import _now_ms, ensure_fresh, is_fresh, request_snapshot
from src.api.guards import (
    initiator_for,
    read_force_body,
    require_api_caller,
    require_not_paused,
    require_operational,
)
from src.db.actions import (
    insert_action,
    mark_action_abandoned,
    normalize_url,
    set_restored_at,
)
from src.ext import protocol
from src.ext.commands import CommandError, send_command

# §6 codes from a failed ``open_tab`` that mean "your picture is stale / the edge
# refused" — answered 409 + refetch, like ``/api/focus`` and the manual reset. Anything
# else (no_connection, timeout, internal) is a transport-class 502.
_OPEN_CLIENT_ERRORS = frozenset(
    {protocol.ERR_PRECONDITION_FAILED, protocol.ERR_STALE_SESSION, protocol.ERR_NO_SUCH_TAB}
)

# Back-compat aliases: the freshness primitives moved to src.api.freshness (ONE
# mechanism, see that module's docstring). Kept importable from here because this is
# where they were born and other modules/tests may still name them.
_is_fresh = is_fresh
_request_snapshot = request_snapshot


# --- read helpers (short-lived reader connection) ---------------------------
def _read_action(conn: sqlite3.Connection, action_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM actions WHERE id = ?", (action_id,)
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
async def _ensure_fresh(registry, db, instance_id: str, settings):
    """Return the live ConnState once the source is fresh, else raise 409.

    Never returns a substitute instance — the whole point of the explicit error
    is that a non-fresh source must NOT silently become ``main`` (§10).

    Delegates to the SHARED :func:`src.api.freshness.ensure_fresh`, which waits on a
    foreign ``pending_snapshot_id`` instead of overwriting it: a restore issued while
    a pass is collecting snapshots must not eject the instance from that pass.
    """
    fresh, reason, conn_state = await ensure_fresh(registry, db, instance_id, settings)
    if fresh:
        return conn_state
    if reason == "disconnected":
        raise HTTPException(
            status_code=409,
            detail=(
                f"instance {instance_id!r} is not connected; refusing to restore "
                "(would otherwise open the tab in the wrong instance)"
            ),
        )
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


def _find_live_relocate(conn: sqlite3.Connection, orig: sqlite3.Row):
    """The UNFINISHED phase-A ``relocate`` row this restore cancels, or ``None``.

    Targets it PRECISELY by id, never by a ``url_norm`` scan: §4 keeps no intermediate
    status, so a *completed* past relocation of the same URL also stays
    ``status='done', restored_at=NULL`` forever — an ``ORDER BY ts DESC`` scan could
    abandon that wrong row (archive corruption + a bogus target exemption). The exact
    link is known: restoring a phase-B close carries ``origin_action_id`` to its
    phase-A relocate (§4); restoring the relocate row itself is its own id.
    """
    candidate_id = orig["origin_action_id"]
    if candidate_id is None:
        candidate_id = orig["id"]
    return conn.execute(
        "SELECT id, instance_to FROM actions "
        "WHERE id = ? AND kind = 'relocate' AND status = 'done' "
        "AND restored_at IS NULL",
        (candidate_id,),
    ).fetchone()


def _read_exemption(conn: sqlite3.Connection, instance_id: str, url: str):
    return conn.execute(
        "SELECT until, reason FROM exemptions WHERE instance_id = ? AND url = ?",
        (instance_id, url),
    ).fetchone()


def _pre_open_exemptions(
    conn: sqlite3.Connection, orig: sqlite3.Row, instance_from: str, url: str, until: int
) -> list:
    """Write the restore exemptions BEFORE the ``open_tab`` command (§10).

    §10 wants the exemption "in one transaction with the open"; the open is a WS
    command that cannot join a transaction, so the ORDER is chosen to fail safe. If
    the service dies between a successful open and the recording write, the reopened
    tab is still protected and the next pass does not evict it straight back. The
    reverse order left exactly that hole.

    Both sides are covered here, as after the open: source and — for an unfinished
    relocation — the relocation target. The relocate row itself is NOT touched yet
    (abandoning it is a state change that belongs with the completed restore).

    Returns the UNDO LOG — ``[(instance_id, url, prior_row_or_None), …]`` — because
    "harmless, it expires by itself" only holds for a crash. When ``open_tab`` fails
    NORMALLY (the extension is wedged, the command times out) the tab is not back, and
    leaving the exemption would take that URL out of curation for
    ``RESTORE_EXEMPTION_MIN`` on the strength of an action that did not happen. The
    caller rolls back with :func:`_rollback_exemptions`, which restores the PRIOR row
    rather than deleting — an exemption written by an earlier restore must survive this
    one's failure.
    """
    saved = []
    targets = [instance_from]
    reloc = _find_live_relocate(conn, orig)
    if reloc is not None and reloc[1]:
        targets.append(reloc[1])
    for inst in targets:
        saved.append((inst, url, _read_exemption(conn, inst, url)))
        _upsert_exemption(conn, inst, url, until)
    return saved


def _rollback_exemptions(conn: sqlite3.Connection, saved: list) -> None:
    """Undo :func:`_pre_open_exemptions` after a failed open — restoring, not deleting.

    A pair that had NO row before is removed; a pair that had one is put back with its
    original ``until``/``reason``, so a still-valid protection from an earlier restore is
    not destroyed by this attempt.
    """
    for instance_id, url, prior in saved:
        if prior is None:
            conn.execute(
                "DELETE FROM exemptions WHERE instance_id = ? AND url = ?",
                (instance_id, url),
            )
        else:
            conn.execute(
                "UPDATE exemptions SET until = ?, reason = ? "
                "WHERE instance_id = ? AND url = ?",
                (prior[0], prior[1], instance_id, url),
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
    detail: str = "restore",
    initiator: str = "user",
) -> int:
    """Atomic DB side of restore. Returns the new restore action's id.

    The exemptions were already written by :func:`_pre_open_exemptions` before the
    open; upserting them again here REFRESHES ``until`` from the moment the restore
    actually completed and keeps this write self-sufficient.
    """
    # (a) exemption on the source we reopened in — else the next pass re-evicts it.
    _upsert_exemption(conn, instance_from, url, until)

    # (b) cancel an UNFINISHED relocation for this url (§10).
    reloc = _find_live_relocate(conn, orig)
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

    # (c) the restore action row (§10: kind='restore', origin_action_id = the restored
    # row). instance_to = where it was reopened. ``initiator`` is 'user' for the human
    # (instance secret) or 'admin' for an ADMIN_TOKEN caller (§35 §5).
    restore_id = insert_action(
        conn,
        ts=now,
        kind="restore",
        status="done",
        initiator=initiator,
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
        detail=detail,
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
async def restore_row(
    app,
    orig,
    *,
    initiator: str = "user",
    write_on_present: bool = False,
    forced: bool = False,
) -> dict:
    """Reopen the source tab recorded by one archived ``actions`` row (§10).

    THE restore mechanism, factored out of the endpoint so pass-undo reuses it per
    row instead of reimplementing it. Freshens the source (§6, never a silent
    ``main``), dedups by URL, writes the exemptions (BEFORE the open — see the module
    docstring, step 5), opens the tab OUTSIDE any transaction, then records the
    restore row + ``restored_at`` + (for an unfinished relocation) the ``relocate``
    cancellation in ONE atomic ``db.write`` (via :func:`_record_restore`).

    ``write_on_present`` — when the URL is ALREADY live in the source (dedup hit):
    the endpoint returns without writing (idempotent restore), but pass-undo passes
    ``True`` so the exemptions + ``restored_at`` markers are STILL written — otherwise
    undoing a relocation whose source was never closed would leave no exemption and
    the next pass would re-evict it ("undo looks broken", §10).

    ``forced`` — this restore ran through an ARMED pause via the human's explicit
    ``force:true`` (§7). It changes nothing about the mechanics; it only marks the
    archive row's ``detail`` so a forced action is distinguishable afterwards (§7 asks
    for exactly that, and forbids a new column).

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
    if present and not write_on_present:
        # Endpoint path: already present => nothing to open and nothing to record.
        return {"restored": False, "reason": "already_present", "action_id": None, "tab_id": None}

    # Exemptions FIRST, in their own transaction, BEFORE the open (§10 / step 5 of the
    # module docstring): a crash after a successful open must not leave the reopened
    # tab unprotected. The write returns an undo log for the OTHER outcome — a normal
    # command failure, where nothing was reopened and the protection must not stand.
    pre_until = _now_ms() + settings.restore_exemption_min * 60_000
    saved = await db.write(
        lambda c: _pre_open_exemptions(c, orig, instance_from, url, pre_until)
    )

    new_tab_id = None
    if not present:
        # Open the tab (async command, OUTSIDE any transaction).
        try:
            result = await send_command(
                registry,
                db,
                instance_from,
                protocol.CMD_OPEN_TAB,
                _open_tab_params(orig, url),
                cmd_timeout_ms=settings.cmd_timeout_ms,
                initiator=initiator,
            )
        except CommandError as exc:
            # The tab did NOT come back. Two things must not happen: a 500 (this is a
            # §6 outcome, not a bug — and pass-undo, which isolates failures per row,
            # only catches HTTPException), and a two-hour exemption earned by an action
            # that never took place. Roll the protection back to exactly what it was.
            await db.write(lambda c: _rollback_exemptions(c, saved))
            raise HTTPException(
                status_code=409 if exc.code in _OPEN_CLIENT_ERRORS else 502,
                detail={
                    "error": exc.code,
                    "message": exc.message,
                    "refetch": exc.code in _OPEN_CLIENT_ERRORS,
                },
            )
        new_tab_id = result.get("tabId")

    # Recomputed (not reused from `pre_until`) so the committed deadline counts from the
    # moment the restore actually completed, not from before the open went out.
    now = _now_ms()
    until = now + settings.restore_exemption_min * 60_000
    detail = "restore:force" if forced else "restore"
    restore_id = await db.write(
        lambda c: _record_restore(
            c, orig, instance_from, url, url_norm, new_tab_id, session_now, now, until,
            detail, initiator,
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
    caller = await require_api_caller(request)  # 401 before anything else
    require_operational(request)    # 503 in degraded mode
    # §7: a pause silences ALL automation, but restore is one of the human's OWN
    # buttons, so an explicit `force:true` in the body may cross the gate. Read the
    # body BEFORE the gate so the flag can be seen. force is honoured only for the
    # instance caller (the human at the startpage); an admin's force cannot cross (§35 §4).
    body = await read_force_body(request)
    forced = body.get("force") is True and caller.kind == "instance"
    await require_not_paused(request, force=forced)

    action_id = request.path_params["action_id"]
    orig = await request.app.state.db.read(lambda c: _read_action(c, action_id))
    if orig is None:
        raise HTTPException(status_code=404, detail=f"action {action_id} not found")

    res = await restore_row(
        request.app, orig, initiator=initiator_for(caller), forced=forced
    )
    if res["restored"]:
        return JSONResponse(
            {"ok": True, "restored": True, "action_id": res["action_id"], "tab_id": res["tab_id"]}
        )
    return JSONResponse({"ok": True, "restored": False, "reason": res["reason"]})
