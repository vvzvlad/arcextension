"""``POST /api/passes/:pass_id/undo`` — roll one pass back (§10).

Undo is NOT a new mechanism: it is :func:`~src.api.restore.restore_row` applied to
each REVERSIBLE row of the pass, in REVERSE ``ts`` order, each writing ``exemptions``
— otherwise undo would bring the tabs back only for the next pass to evict them again
("undo looks broken", §10). Per ``kind`` (§10 table):

* ``relocate`` (phase A) — the relocation is reversed as a UNIT: reopen the source in
  ``instance_from`` (``restore_row``) AND close the copy ``tab_id_to`` in
  ``instance_to`` WITH the step-4 close guards (the extension re-verifies
  url/not-audible/not-pinned/active-in-focus/min-idle at the edge via ``expect``).
* ``relocate_close`` — the SAME relocation unit, found via ``origin_action_id`` (§10:
  "undoing either half finds the other"). The reopen uses the phase-A ``relocate``
  row (it carries the source clocks + title), so undoing either half yields exactly
  "tab in source, copy gone" regardless of which ``pass_id`` was dragged.
* ``dedupe_close`` / ``singleton_close`` — a pure close: reopen the source only.
* ``restore`` — skipped (undoing a return is meaningless).
* ``window_merge`` — un-undoable (§9); REPORTED in the response, never silently dropped.
* ``deferred`` — nothing happened; nothing to undo.
* rows with ``restored_at`` already set — skipped (already undone; keeps undo idempotent).

The response is a PER-ROW summary, not ``{ok}`` — partial undo is the norm, not a
failure (§10). Undo obeys the same ``confirm_impact`` gate as a rule edit (§8/§10): a
server-side preview of the impact, and a 409 carrying it when impact>0 without
``confirm_impact:true``.

Session guard (§5/§10): the reopen always works BY URL (``restore_row`` never closes a
tab by ``tab_id``); the copy-close is the only ``tab_id``-addressed step, and it is
SKIPPED when the target's live session differs from ``session_id_to`` — a ``tab_id``
from a dead session addresses a FOREIGN tab.
"""

from __future__ import annotations

import sqlite3

from loguru import logger
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import (
    read_force_body,
    require_ext_token,
    require_not_paused,
    require_operational,
)
from src.api.restore import _now_ms, _read_action, restore_row
from src.db.actions import insert_action, set_action_status
from src.ext import protocol
from src.ext.commands import CommandError, send_command


# --- reads (short-lived reader connection) ----------------------------------
def _read_pass_actions(conn: sqlite3.Connection, pass_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM actions WHERE pass_id = ?", (pass_id,)
    ).fetchall()


def _pass_row_exists(conn: sqlite3.Connection, pass_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM passes WHERE pass_id = ?", (pass_id,)
        ).fetchone()
        is not None
    )


# --- small sync writers (run inside a guarded Database.write) ----------------
def _mark_linked_closes(conn: sqlite3.Connection, reloc_id: int, now: int) -> None:
    """Stamp ``restored_at`` on every ``relocate_close`` that completes ``reloc_id``.

    ``_record_restore`` (reused via ``restore_row``) already cancels the ``relocate``
    row and stamps IT; this closes the loop from the OTHER direction — when a
    relocation is undone via its phase-A ``relocate`` row, its phase-B ``relocate_close``
    (possibly in a DIFFERENT pass) must also be marked so undoing that other pass later
    skips the paired half instead of double-restoring (§10)."""
    conn.execute(
        "UPDATE actions SET restored_at = ? "
        "WHERE origin_action_id = ? AND kind = 'relocate_close' AND restored_at IS NULL",
        (now, reloc_id),
    )


def _delete_tab(conn: sqlite3.Connection, instance_id: str, tab_id: int) -> None:
    """Drop the closed copy from the mirror so the next pass / archive view sees it
    gone at once (the next snapshot would remove it anyway)."""
    conn.execute(
        "DELETE FROM tabs WHERE instance_id = ? AND tab_id = ?", (instance_id, tab_id)
    )


# --- classification / preview (pure over the pass rows) ---------------------
_PURE_CLOSE_KINDS = ("dedupe_close", "singleton_close")


def _classify(rows: list) -> dict:
    """Model the undo's impact from the archive alone (a static preview, §10).

    A relocation is ONE unit even when both halves are in the pass: the phase-A
    ``relocate`` counts it; a phase-B ``relocate_close`` counts it ONLY when its
    ``relocate`` is in another pass (otherwise it would double-count)."""
    reversible = [r for r in rows if r["restored_at"] is None]
    reloc_ids = {
        r["id"] for r in reversible if r["kind"] == "relocate" and r["status"] == "done"
    }
    units: set = set(reloc_ids)
    pure_closes = 0
    un_undoable: list = []
    for r in reversible:
        kind, status = r["kind"], r["status"]
        if kind == "relocate_close" and status == "done":
            if r["origin_action_id"] not in reloc_ids:
                units.add(("close", r["id"]))  # its phase A is elsewhere
        elif kind in _PURE_CLOSE_KINDS and status == "done":
            pure_closes += 1
        elif kind == "window_merge":
            un_undoable.append(
                {"action_id": r["id"], "kind": "window_merge", "url": r["url"]}
            )
    relocations = len(units)
    reopens = relocations + pure_closes
    copy_closes = relocations
    return {
        "relocations": relocations,
        "pure_closes": pure_closes,
        "reopens": reopens,
        "copy_closes": copy_closes,
        # Impact = every physical operation the undo would perform (reopens + closes).
        # The confirm gate fires on impact>0, exactly like a rule edit (§8/§10).
        "impact": reopens + copy_closes,
        "un_undoable": un_undoable,
    }


def _preview_payload(pass_id: str, cls: dict) -> dict:
    return {
        "pass_id": pass_id,
        "impact": cls["impact"],
        "reopens": cls["reopens"],
        "copy_closes": cls["copy_closes"],
        "relocations": cls["relocations"],
        "pure_closes": cls["pure_closes"],
        "un_undoable": cls["un_undoable"],
        "requires_confirm": cls["impact"] > 0,
    }


# --- the copy-close (step-4 guards + §5 session guard) ----------------------
def _write_pending_undo_close(
    conn: sqlite3.Connection, reloc: sqlite3.Row, *, now: int, detail: str
) -> int:
    """Journal the copy-close BEFORE the browser command, as ``pending`` (Фаза 16).

    This close used to be the ONLY one in the system performed outside the journal:
    it sent ``close_tab`` and then just ``DELETE FROM tabs``. The at-least-once
    discipline the pass uses for its own closes applies here for the same reason — a
    process death between a successful close and the completion write must not erase
    the evidence that a tab was closed.

    The row describes the tab actually being closed: ``instance_from`` /
    ``tab_id`` / ``session_id_from`` are the COPY's side (``instance_to`` /
    ``tab_id_to`` / ``session_id_to`` of the relocation), and ``origin_action_id``
    points at the phase-A ``relocate`` being reversed.
    """
    return insert_action(
        conn,
        ts=now,
        kind="undo_close",
        status="pending",
        initiator="user",
        origin_action_id=reloc["id"],
        instance_from=reloc["instance_to"],
        tab_id=reloc["tab_id_to"],
        session_id_from=reloc["session_id_to"],
        url=reloc["url"],
        url_norm=reloc["url_norm"],
        title=reloc["title"],
        decision="undo",
        detail=detail,
    )


async def _close_copy(app, reloc: sqlite3.Row, idle_ms: int, *, forced: bool = False) -> dict:
    """Close the phase-A copy ``tab_id_to`` in ``instance_to`` WITH the step-4 guards.

    Reuses the guarded close path: a ``close_tab`` carrying the full ``expect`` so the
    extension re-verifies url / not-audible / not-pinned / active-in-focus / min-idle
    at ``tabs.remove`` time (§7 step 4). The §5 session guard is enforced HERE: a
    ``tab_id`` recorded under ``session_id_to`` addresses a FOREIGN tab once the target
    reconnected under a new session, so on a mismatch we SKIP the close entirely and
    let the reopen (which is by URL) stand alone.

    Journaled at-least-once (Фаза 16): an ``undo_close`` row is written ``pending``
    AFTER the guards and BEFORE the command leaves, then completed (``done``) or failed
    (``failed`` + the §6 code) once the outcome is known. The skip branches write no
    row — no command was sent, so nothing happened to record.

    LIMITATION, stated rather than papered over: those two are the only paths THIS
    function can take, but a hard process death between the ``pending`` write and the
    completion leaves the row ``pending`` for good. Nothing reconciles it — the pass's
    reconcile step (``read_pending_closes``) deliberately covers only its own three close
    kinds, whose ``instance_from`` means "the source of a relocation", while here it
    means "the side the copy lived on". The row is inert everywhere (no reader keys on
    ``undo_close``; retention sweeps it with the rest of ``actions``), so the cost is
    cosmetic: the archive shows a close whose outcome was never determined — which is
    exactly what happened, and strictly more than the nothing it recorded before."""
    registry = app.state.ext_registry
    settings = app.state.settings
    db = app.state.db
    instance_to = reloc["instance_to"]
    tab_id_to = reloc["tab_id_to"]
    session_id_to = reloc["session_id_to"]
    url = reloc["url"]
    if not instance_to or tab_id_to is None:
        return {"copy_closed": False, "reason": "no_copy"}
    conn_state = registry.get(instance_to)
    if conn_state is None:
        return {"copy_closed": False, "reason": "target_disconnected"}
    # §5/§10: never close by a tab_id from a dead session (it names a foreign tab).
    if session_id_to is not None and conn_state.session_id != session_id_to:
        return {"copy_closed": False, "reason": "session_mismatch"}
    expect = {
        "url": url,
        "notAudible": True,
        "notPinned": True,
        "minIdleMs": idle_ms,
    }
    detail = "undo_close:force" if forced else "undo_close"
    action_id = await db.write(
        lambda c: _write_pending_undo_close(c, reloc, now=_now_ms(), detail=detail)
    )
    try:
        await send_command(
            registry,
            db,
            instance_to,
            protocol.CMD_CLOSE_TAB,
            {"tabId": tab_id_to, "expect": expect},
            cmd_timeout_ms=settings.cmd_timeout_ms,
            initiator="user",
        )
    except CommandError as exc:
        # precondition_failed (the human is using the copy), no_such_tab (already gone),
        # or a connection-class code — all reported, none aborts the pass undo.
        await db.write(
            lambda c: set_action_status(c, action_id, "failed", reason=exc.code)
        )
        return {"copy_closed": False, "reason": exc.code, "action_id": action_id}

    def _done(conn: sqlite3.Connection) -> None:
        set_action_status(conn, action_id, "done")
        _delete_tab(conn, instance_to, tab_id_to)

    await db.write(_done)
    return {"copy_closed": True, "action_id": action_id}


# --- per-row undo -----------------------------------------------------------
async def _isolated(coro, entry: dict) -> dict:
    """Run ONE row's undo; never let it abort the whole pass-undo.

    §10 makes the response a per-row summary precisely because "partial undo is the
    norm, not a failure". An exception escaping one row would replace that summary with
    a 500 — after some rows had already been reversed, ``restored_at`` stamped and copies
    closed — leaving the human with no record of what did happen. The expected failures
    are already turned into HTTPException by ``restore_row``; this is the backstop for
    anything else, and it REPORTS rather than hides (the row is marked failed with the
    exception's type and message).
    """
    try:
        return await coro
    except HTTPException as exc:
        return {**entry, "outcome": "failed", "reason": _http_reason(exc)}
    except Exception as exc:  # noqa: BLE001 - one bad row must not eat the summary
        logger.exception("pass undo: row {} failed unexpectedly", entry.get("action_id"))
        return {
            **entry,
            "outcome": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _http_reason(exc: HTTPException) -> str:
    """A short, readable per-row reason for the §10 summary.

    ``restore_row`` raises a DICT detail for a failed ``open_tab`` (``{error, message,
    …}``); rendering that dict raw would put a Python repr in the response, so the §6
    code is lifted out and the rest is dropped. String details pass through as before.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("error") or f"http_{exc.status_code}"
        message = detail.get("message")
        return f"{exc.status_code}: {code}" + (f" ({message})" if message else "")
    return f"{exc.status_code}: {detail}"


async def _undo_relocation(
    app, reloc: sqlite3.Row, trigger: sqlite3.Row, idle_ms: int, *, forced: bool = False
) -> dict:
    """Reverse a relocation as a unit: reopen the source, then close the copy.

    Source FIRST (data safety): if the reopen cannot be made fresh (§6) we do NOT
    close the copy — closing it after a failed reopen would delete the last copy of
    the URL. ``write_on_present=True`` so the exemptions + ``restored_at`` markers are
    written even when the source tab is still live (an unfinished relocation)."""
    result = {
        "action_id": trigger["id"],
        "kind": trigger["kind"],
        "relocate_id": reloc["id"],
    }
    try:
        rr = await restore_row(app, reloc, write_on_present=True, forced=forced)
    except HTTPException as exc:
        # Source not restorable => leave the copy in place; report and move on.
        result["outcome"] = "failed"
        result["reason"] = _http_reason(exc)
        return result
    result["reopened"] = rr["restored"]
    result["source_already_present"] = rr["reason"] == "already_present"
    # Mark the paired phase-B close(s) undone so undoing their pass later skips them.
    await app.state.db.write(lambda c: _mark_linked_closes(c, reloc["id"], _now_ms()))
    close = await _close_copy(app, reloc, idle_ms, forced=forced)
    result["copy_closed"] = close["copy_closed"]
    result["copy_reason"] = close.get("reason")
    result["outcome"] = "undone"
    return result


async def _undo_pure_close(app, row: sqlite3.Row, *, forced: bool = False) -> dict:
    """Reverse a ``dedupe_close`` / ``singleton_close``: reopen the source only (there
    is no copy to close — these evictions never opened one)."""
    result = {"action_id": row["id"], "kind": row["kind"]}
    try:
        rr = await restore_row(app, row, write_on_present=True, forced=forced)
    except HTTPException as exc:
        result["outcome"] = "failed"
        result["reason"] = _http_reason(exc)
        return result
    result["reopened"] = rr["restored"]
    result["source_already_present"] = rr["reason"] == "already_present"
    result["outcome"] = "undone"
    return result


async def _resolve_relocate(app, rows_by_id: dict, origin_id):
    """The phase-A ``relocate`` row a ``relocate_close`` completes — from this pass's
    rows when present, else read by id (phase A can be in an EARLIER pass, §7)."""
    if origin_id is None:
        return None
    if origin_id in rows_by_id:
        return rows_by_id[origin_id]
    return await app.state.db.read(lambda c: _read_action(c, origin_id))


# --- the endpoint -----------------------------------------------------------
# The optional-JSON-object body reader lives in guards (the pause gate has to read the
# body before it decides, so the two cannot be separate parsers).
_body = read_force_body


async def undo_pass(request: Request) -> JSONResponse:
    require_ext_token(request)      # 401 before anything else
    require_operational(request)    # 503 in degraded mode

    # Body BEFORE the pause gate: undo is one of the human's own buttons, so §7 lets
    # an explicit force:true cross an armed pause (and only that).
    body = await _body(request)
    forced = body.get("force") is True
    await require_not_paused(request, force=forced)  # 423 while paused (§7 gate)

    pass_id = request.path_params["pass_id"]
    app = request.app
    db = app.state.db
    idle_ms = app.state.settings.idle_minutes * 60_000

    rows = await db.read(lambda c: _read_pass_actions(c, pass_id))
    if not rows and not await db.read(lambda c: _pass_row_exists(c, pass_id)):
        raise HTTPException(status_code=404, detail=f"pass {pass_id!r} not found")

    cls = _classify(rows)
    # Confirm gate (§8/§10): the biggest one-time op in the system needs the same
    # confirmation a single rule edit does. 409 carries the preview to re-submit with.
    if cls["impact"] > 0 and not body.get("confirm_impact"):
        payload = _preview_payload(pass_id, cls)
        payload["error"] = "confirm_impact required"
        return JSONResponse(payload, status_code=409)

    rows_by_id = {r["id"]: r for r in rows}
    reloc_ids_in_pass = {
        r["id"] for r in rows if r["kind"] == "relocate" and r["status"] == "done"
    }
    handled: set = set()  # relocate ids already reversed via their phase-B half
    results: list = []
    un_undone: list = []
    skipped: list = []

    # REVERSE ts order (newest first); id breaks ties deterministically (§10).
    ordered = sorted(rows, key=lambda r: (r["ts"], r["id"]), reverse=True)
    for r in ordered:
        rid, kind, status = r["id"], r["kind"], r["status"]
        entry = {"action_id": rid, "kind": kind}
        if r["restored_at"] is not None:
            skipped.append({**entry, "reason": "already_undone"})
            continue
        if kind == "window_merge":
            un_undone.append({**entry, "reason": "window_merge_not_undoable"})
            continue
        if kind == "restore":
            skipped.append({**entry, "reason": "restore_not_undone"})
            continue
        if status == "deferred":
            skipped.append({**entry, "reason": "deferred_nothing"})
            continue
        if status != "done":
            # A failed close never removed a tab; there is nothing to reverse.
            skipped.append({**entry, "reason": f"status_{status}"})
            continue
        if kind == "relocate":
            if rid in handled:
                skipped.append({**entry, "reason": "paired_already_undone"})
                continue
            results.append(
                await _isolated(_undo_relocation(app, r, r, idle_ms, forced=forced), entry)
            )
            handled.add(rid)
        elif kind == "relocate_close":
            reloc = await _resolve_relocate(app, rows_by_id, r["origin_action_id"])
            if reloc is None:
                # Orphan close (no phase-A row found): reopen the source from this row.
                results.append(
                    await _isolated(_undo_pure_close(app, r, forced=forced), entry)
                )
                continue
            if reloc["id"] in handled or reloc["restored_at"] is not None:
                skipped.append({**entry, "reason": "paired_already_undone"})
                continue
            results.append(
                await _isolated(
                    _undo_relocation(app, reloc, r, idle_ms, forced=forced), entry
                )
            )
            handled.add(reloc["id"])
        elif kind in _PURE_CLOSE_KINDS:
            results.append(
                await _isolated(_undo_pure_close(app, r, forced=forced), entry)
            )
        else:
            skipped.append({**entry, "reason": f"kind_{kind}"})

    reopened = sum(1 for x in results if x.get("reopened"))
    copies_closed = sum(1 for x in results if x.get("copy_closed"))
    failed = sum(1 for x in results if x.get("outcome") == "failed")
    return JSONResponse(
        {
            "pass_id": pass_id,
            "results": results,        # per-row summary (NOT {ok}) — partial undo is normal
            "un_undone": un_undone,    # window_merge et al. honestly reported (§9/§10)
            "skipped": skipped,
            "counts": {
                "reopened": reopened,
                "copies_closed": copies_closed,
                "un_undone": len(un_undone),
                "skipped": len(skipped),
                "failed": failed,
            },
        }
    )
