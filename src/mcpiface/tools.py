"""MCP tool handlers (§11) — thin adapters over already-built logic.

Every handler is a plain ``async def`` taking the HOST app (for ``app.state.db`` /
``ext_registry`` / ``settings``) plus its tool arguments, so each can be unit-tested
directly without an MCP client or a socket. NOTHING here reimplements state, rules,
command, pass or pause logic — the handlers only call the reused functions:

* reads     -> :mod:`src.db.state`, :mod:`src.rules.access`, :mod:`src.api.actions`
* freshness -> :func:`src.api.freshness.ensure_fresh` fanned across the active fleet
  (the BLOCKING-fresh class §6, same as restore/preview/reset — NOT ``/api/state``'s
  detached kick), so ``list_tabs`` / ``list_instances`` return a mirror the agent has
  just refreshed rather than one of arbitrary age
* rules CRUD + the SAME ``confirm_impact`` gate -> :mod:`src.api.rules`
* commands  -> :func:`src.ext.commands.send_command` (``initiator='mcp'`` + ``auth_ctx``)
* relocate  -> phase-A open + a live ``relocate`` row the pass's phase B completes
* run_pass  -> :func:`src.curator.runner.run_pass`
* pause     -> :mod:`src.curator.pause` (settings write + ``lease.bump_epoch``)

§12: a paused system REFUSES mutating verbs. Reads and ``pause`` / ``resume`` are
never gated; ``run_pass`` handles pause itself (a dry_run is never muted — looking at
the plan is exactly why a pause is taken).
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from types import SimpleNamespace

from starlette.exceptions import HTTPException

from src.api import actions as actions_api
from src.api import instances as instances_api
from src.api import pause as pause_api
from src.api import rules as rules_api
from src.api.freshness import ERROR, ensure_fresh
from src.curator import pause as pause_ops
from src.curator import runner
from src.db import state as state_read
from src.db.actions import insert_action, normalize_url
from src.db.settings_store import get_setting
from src.ext import protocol
from src.ext.commands import CommandError, send_command
from src.rules import access as rules_access


def _now_ms() -> int:
    return int(time.time() * 1000)


class ToolError(Exception):
    """A tool refused or failed in a way the agent must see (returned, not raised
    out of the transport). ``code`` is a short machine string; ``payload`` carries
    any structured context (a preview, a pause deadline)."""

    def __init__(self, code: str, message: str, payload: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.payload = payload or {}
        super().__init__(f"{code}: {message}")


# --- pause gate (§12) --------------------------------------------------------
async def _ensure_not_paused(app) -> None:
    """Raise :class:`ToolError` when a pause is armed — a paused system refuses every
    mutating MCP verb (§12). Reused by all command/rule-write/relocate/reset verbs."""
    until = await app.state.db.read(pause_ops.read_pause_until)
    if until is not None and until > _now_ms():
        raise ToolError(
            "paused",
            "the curator is paused; mutating verbs are refused until resume",
            {"paused_until": until},
        )


# --- reads -------------------------------------------------------------------
async def _freshen_fleet(app) -> dict:
    """Actively refresh EVERY active instance's mirror and return the per-instance
    freshness envelope ``{id: {snapshot_at, fresh, reason, session_id}}`` (§6/§11).

    ``session_id`` (#47 "session epoch") is each active instance's current
    ``instances.session_id`` — the epoch an agent echoes back as ``expected_session``
    to a mutating verb so a command minted against a now-dead session is refused
    (``stale_session``) instead of closing the wrong tab after a browser restart.

    ``list_tabs`` / ``list_instances`` join the restore/preview/reset (blocking-fresh)
    class (§6), NOT ``/api/state``'s detached kick: an agent that acts on a mirror of
    arbitrary age re-opens a tab a human opened three minutes ago — permanently, in
    ``main`` where there is no dedup (§11). So we WAIT for a fresh snapshot instead of
    firing one and reading the stale mirror immediately.

    Instance set is :func:`known_instance_ids` (every ``status='active'`` instance —
    the same set preview fans over), NOT "only connected": ``ensure_fresh`` itself
    reports ``disconnected`` for an instance without a live socket, whereas a
    connected-only set would silently DROP an instance whose DB row is a stale
    ``connected=0`` and hide it from the agent entirely.

    Fanned out CONCURRENTLY with ONE ``SNAPSHOT_TIMEOUT_MS`` budget each (the concurrent-
    with-budget shape of the preview fan-out in :mod:`src.api.rules`; here deliberately
    hardened with ``return_exceptions=True``, which the preview does not use): each call
    only awaits its own socket and polls its own reader connection, so N wedged instances
    cost ONE budget of wall time, not N.

    ``return_exceptions=True`` is MANDATORY: ``ensure_fresh`` awaits ``db.read`` unwrapped,
    so a reader fault (``database is locked``, disk I/O) propagates; without it that one
    fault would sink the whole tool and leave the sibling tasks hanging. A raised result
    is mapped to ``fresh: false, reason: "error"`` and the siblings are returned normally.

    ``snapshot_at`` is each instance's stored value, read AFTER the fan-out so it reflects
    any snapshot that just landed (``null`` when the instance has never been snapshotted).
    Both ``fresh`` and ``reason`` are reported even though ``fresh == (reason == "fresh")``:
    ``reason`` names WHY (``disconnected`` / ``timeout`` / ``error``) so the agent can act.
    """
    db, settings, registry = app.state.db, app.state.settings, app.state.ext_registry
    known = sorted(await db.read(rules_access.known_instance_ids))
    results = await asyncio.gather(
        *(
            ensure_fresh(registry, db, iid, settings, budget_ms=settings.snapshot_timeout_ms)
            for iid in known
        ),
        return_exceptions=True,
    )
    # snapshot_at read after the waits so a just-landed snapshot is reflected.
    snapshot_at = {
        i["id"]: i["snapshot_at"] for i in await db.read(state_read._read_instances)
    }
    # session_id per active instance (#47): the epoch stamped alongside freshness so the
    # agent can pin it as expected_session on a later mutating verb.
    sessions = await db.read(state_read._read_active_sessions)
    envelope: dict = {}
    for iid, res in zip(known, results):
        # Only a reader FAULT (a sqlite Exception from the unwrapped db.read inside
        # ensure_fresh) becomes "error"; a control-flow BaseException (CancelledError
        # on tool cancellation) is left to propagate rather than mislabelled as a
        # per-instance error. Outer cancellation raises out of gather() before this
        # loop, so such a result never lands here.
        if isinstance(res, Exception):
            fresh, reason = False, ERROR
        else:
            fresh, reason, _conn_state = res
        envelope[iid] = {
            "snapshot_at": snapshot_at.get(iid),
            "fresh": fresh,
            "reason": reason,
            "session_id": sessions.get(iid),
        }
    return envelope


async def list_instances(app) -> dict:
    """Per-instance freshness + ``paused_until`` + ``resume_pending`` + ``pending_plan`` (§11).

    Awaits a fresh snapshot from every active instance (§6 blocking-fresh, via
    :func:`_freshen_fleet`) before answering, so the ``instances`` map carries a mirror
    the agent has just refreshed — each entry ``{snapshot_at, fresh, reason, session_id}``
    (``session_id`` is the #47 epoch to echo back as ``expected_session``). The
    ``paused_until`` field lets the agent tell a pause from a broken curator (§11).

    ``resume_pending`` is required here by §7 verbatim — «`resume_pending` виден в
    `StateResponse` и в `list_instances`» — and for the same reason: after a pause
    expires by TIMEOUT the curator is deliberately idle, waiting for a confirming click.
    Without the flag an agent reads ``paused_until`` in the past, sees no pass
    happening, and concludes the curator is broken. ``pending_plan`` is the SAME
    resume/pending-plan shape the startpage already sees in ``StateResponse`` (parsed
    by :func:`src.db.state._parse_pending_plan`) — the burst the human confirms the
    click BY — surfaced to the agent that only had the bare boolean before."""
    db = app.state.db
    instances = await _freshen_fleet(app)
    paused_until = await db.read(pause_ops.read_pause_until)
    resume_raw = await db.read(lambda c: get_setting(c, pause_ops.RESUME_PENDING_KEY))
    return {
        "server_now": _now_ms(),
        "paused_until": paused_until,
        "resume_pending": bool(resume_raw),
        "pending_plan": state_read._parse_pending_plan(resume_raw),
        "instances": instances,
    }


def _project_tabs(rows: list[dict]) -> list[dict]:
    """Adapter-side (#46) projection over the mirror rows: DROP ``fav_icon_url`` and add
    ``dup_group``.

    ``fav_icon_url`` has zero consumers on the agent side (the startpage draws a CSS
    swatch from the host, not ``<img src=favIconUrl>``), so it is omitted here rather
    than by changing the SQL surface — ``_TAB_COLUMNS`` / ``GET /api/state`` keep it.

    ``dup_group`` is the ``normalize_url(url)`` key (origin+path, no query/fragment),
    set ONLY when MORE THAN ONE tab in THIS output (i.e. after any filters) shares that
    key; the sole holder of a key gets ``None``. Computed in Python because ``tabs`` has
    no ``url_norm`` column (it exists only on ``actions``). The key IS the normalized
    address — readable and stable between calls.
    """
    keys = [normalize_url(r["url"]) for r in rows]
    counts = Counter(k for k in keys if k is not None)
    projected: list[dict] = []
    for row, key in zip(rows, keys):
        tab = {k: v for k, v in row.items() if k != "fav_icon_url"}
        tab["dup_group"] = key if (key is not None and counts[key] > 1) else None
        projected.append(tab)
    return projected


async def list_tabs(app, *, instance: str | None = None, window_id: int | None = None,
                    url_contains: str | None = None) -> dict:
    """Tabs mirror + per-instance freshness envelope (§11), with #46 filters + dup marking.

    Awaits a fresh snapshot from every active instance (§6 blocking-fresh, via
    :func:`_freshen_fleet`) before returning the tabs, so the agent never acts on a
    mirror of unknown age (§11: else it re-opens a tab a human opened three minutes
    ago — permanently, in ``main`` where there is no dedup). The ``instances`` map
    carries ``{snapshot_at, fresh, reason, session_id}`` per instance — the age, freshness
    and (#47) session epoch the agent must weigh before acting (``session_id`` echoes back
    as ``expected_session``).

    Filters (all optional, intersecting) are applied in SQL by :func:`_read_tabs`:
    ``instance`` (exact), ``window_id`` (exact), ``url_contains`` (case-insensitive
    substring of the url). ``window_id`` alone filters across instances but is ambiguous
    — the tabs/windows key is ``(instance_id, window_id)`` — so pass ``instance`` with it
    to name one window.

    Each tab carries ``dup_group``: the normalized address (origin+path) shared by more
    than one tab in the POST-FILTER output, else ``null``. It is an agent-facing HINT,
    not a prediction of what the curator collapses: dup_group groups by the NORMALIZED
    address computed over this output, whereas the curator dedups by the FULL url string
    and only OUTSIDE ``main`` (``main`` is a sink without dedup). ``fav_icon_url`` is NOT
    in this response (no consumer); ``GET /api/state`` still carries it."""
    db = app.state.db
    instances = await _freshen_fleet(app)
    rows = await db.read(
        lambda c: state_read._read_tabs(
            c, instance=instance, window_id=window_id, url_contains=url_contains
        )
    )
    return {
        "server_now": _now_ms(),
        "tabs": _project_tabs(rows),
        "instances": instances,
    }


async def list_windows(app) -> dict:
    """Per-window summary + per-instance freshness envelope (§11, #46).

    One record per window — ``instance_id``, ``window_id``, ``type``, ``state``,
    ``tab_count`` and a ``focused`` flag — sourced from the ``windows`` table, a COUNT
    over ``tabs`` and ``instances.focused_window_id`` (see :func:`_read_windows`). A
    per-window summary (a few hundred bytes for a typical fleet), NOT the per-tab list.

    Same freshness contract and ``instances`` envelope as :func:`list_tabs`: it awaits a
    fresh snapshot per active instance via :func:`_freshen_fleet` before answering."""
    db = app.state.db
    instances = await _freshen_fleet(app)
    windows = await db.read(state_read._read_windows)
    return {
        "server_now": _now_ms(),
        "windows": windows,
        "instances": instances,
    }


async def get_rules(app) -> dict:
    """All rules (§8/§11). Reuses the rules access reader + the API's row mapper."""
    rows = await app.state.db.read(rules_access.list_rules)
    return {"rules": [rules_api._rule_to_dict(r) for r in rows]}


async def list_actions(app, **filters) -> dict:
    """Archive list (§10/§11). Reuses the ``/api/actions`` filter builder + reader.

    ``filters`` accepts the same keys as the HTTP query string (kind, pass_id,
    instance, url, since, limit, offset, deferred) — passed through a plain dict so
    the reused ``_build_filters`` / ``_int_param`` see the exact same shape."""
    params = {k: v for k, v in filters.items() if v is not None}
    limit = actions_api._int_param(
        params, "limit", actions_api._DEFAULT_LIMIT, minimum=1, maximum=actions_api._MAX_LIMIT
    )
    offset = actions_api._int_param(params, "offset", 0, minimum=0, maximum=None)
    clause, args = actions_api._build_filters(params)
    total, items = await app.state.db.read(
        lambda c: actions_api._list_actions(c, clause, args, limit, offset)
    )
    return {"items": items, "total": total}


# --- rules writes (SAME confirm_impact gate as HTTP, §11) --------------------
def _req(app):
    """A minimal request-like stand-in so the reused ``src.api.rules`` helpers
    (they only touch ``request.app``) run unchanged from the MCP path."""
    return SimpleNamespace(app=app)


async def upsert_rule(app, *, rule: dict, confirm_impact: bool = False) -> dict:
    """Create (no ``id``) or update (``id`` present) a rule with the SAME momentous-
    change confirmation as HTTP (§8/§11): return the preview and require a second
    call carrying ``confirm_impact=True`` when the modeled next pass is momentous."""
    await _ensure_not_paused(app)
    req = _req(app)
    fields = rules_api._extract_rule_fields(rule)
    rule_id = rule.get("id")
    op = "update" if rule_id is not None else "create"
    try:
        rules_api._validate_pattern_or_422(fields["pattern"])
        await rules_api._validate_instance_or_422(req, fields["instance_id"])
    except HTTPException as exc:
        raise ToolError("invalid_rule", str(exc.detail))

    current = await rules_api._current_rules(req)
    if op == "update" and not any(r["id"] == rule_id for r in current):
        raise ToolError("not_found", f"rule {rule_id} not found")

    candidate = rules_api._build_candidate(current, op, fields, rule_id)
    res = await rules_api._run_preview(req, candidate)
    if rules_api._requires_confirm(current, candidate, res) and not confirm_impact:
        # The same gate as HTTP's 409: hand back the preview and require the echo.
        return {
            "ok": False,
            "requires_confirm": True,
            "preview": res.to_dict(),
            "not_counted": rules_api._not_counted(res),
            "error": "confirm_impact required",
        }

    db = app.state.db
    if op == "create":
        now = _now_ms()
        new_id = await db.write(
            lambda c: rules_access.insert_rule(
                c, pattern=fields["pattern"], instance_id=fields["instance_id"],
                singleton=fields["singleton"], canonical_url=fields["canonical_url"],
                note=fields["note"], created_at=now,
            )
        )
        return {"ok": True, "id": new_id, "preview": res.to_dict()}

    await db.write(
        lambda c: rules_access.update_rule(
            c, rule_id, pattern=fields["pattern"], instance_id=fields["instance_id"],
            singleton=fields["singleton"], canonical_url=fields["canonical_url"],
            note=fields["note"],
        )
    )
    return {"ok": True, "id": rule_id, "preview": res.to_dict()}


async def delete_rule(app, *, rule_id: int, confirm_impact: bool = False) -> dict:
    """Delete a rule. ALWAYS gated (§8): draining a rule's tabs to main, or disabling
    curation by removing the last rule, are both large and silent — so a delete needs
    ``confirm_impact=True`` after seeing the preview."""
    await _ensure_not_paused(app)
    req = _req(app)
    current = await rules_api._current_rules(req)
    if not any(r["id"] == rule_id for r in current):
        raise ToolError("not_found", f"rule {rule_id} not found")
    candidate = rules_api._build_candidate(current, "delete", {}, rule_id)
    res = await rules_api._run_preview(req, candidate)
    if not confirm_impact:
        return {
            "ok": False,
            "requires_confirm": True,
            "preview": res.to_dict(),
            "not_counted": rules_api._not_counted(res),
            "error": "confirm_impact required",
        }
    await app.state.db.write(lambda c: rules_access.delete_rule(c, rule_id))
    return {"ok": True, "id": rule_id, "preview": res.to_dict()}


async def reset_singleton(app, *, rule_id: int) -> dict:
    """Apply a rule's ``canonical_url`` to its surviving tab (§8/§10).

    The MCP twin of ``POST /api/rules/:id/reset``: it runs the SAME
    :func:`src.api.rules.perform_reset` core, so it has the same semantics and the same
    side effects (a ``navigate_tab`` command and an ``actions(kind='reset')`` row) —
    only ``initiator`` differs, which is precisely what the archive records. A rule
    holding no tab is not an error, just ``reset: false, reason: "no_tabs"``."""
    await _ensure_not_paused(app)
    try:
        return await rules_api.perform_reset(app, rule_id, initiator="mcp")
    except HTTPException as exc:
        raise ToolError(*_tool_error_from_http(exc))


def _tool_error_from_http(exc: HTTPException) -> tuple[str, str]:
    """Map a reused endpoint's ``HTTPException`` onto a ToolError (code, message).

    The agent gets the SHORT machine code the HTTP client would read off the body
    (``not_found``, the §6 command code, …), never a bare status number."""
    detail = exc.detail
    if isinstance(detail, dict):
        return (
            str(detail.get("error") or f"http_{exc.status_code}"),
            str(detail.get("message") or detail.get("error") or exc.status_code),
        )
    if exc.status_code == 404:
        return ("not_found", str(detail))
    if exc.status_code == 409:
        return ("conflict", str(detail))
    if exc.status_code == 422:
        return ("invalid_request", str(detail))
    return (f"http_{exc.status_code}", str(detail))


# --- commands (initiator='mcp' + auth_ctx, §12) ------------------------------
async def _command(app, instance, command, params, *, auth_ctx, expected_session=None):
    """Issue one extension command as an MCP verb. Every command carries
    ``initiator='mcp'`` and ``auth_ctx`` = the MCP session (§12: js_audit records the
    session, never a token id). Command failures are surfaced as a ``ToolError`` so
    the agent sees the §6 error code instead of a transport-level fault — including
    ``stale_session`` (#47), which rides the SAME generic mapping: when the agent pinned
    ``expected_session`` and the browser has since restarted, ``send_command`` stamps the
    old session, the extension refuses with ``stale_session``, and it reaches the agent as
    a ToolError like any other §6 code."""
    settings = app.state.settings
    try:
        return await send_command(
            app.state.ext_registry, app.state.db, instance, command, params,
            cmd_timeout_ms=settings.cmd_timeout_ms, initiator="mcp", auth_ctx=auth_ctx,
            expected_session=expected_session,
        )
    except CommandError as exc:
        raise ToolError(exc.code, exc.message)


async def open_tab(app, *, instance: str, url: str, pinned: bool = False,
                   active: bool = False, auth_ctx: str | None = None,
                   expected_session: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_OPEN_TAB,
        {"url": url, "pinned": bool(pinned), "active": bool(active)}, auth_ctx=auth_ctx,
        expected_session=expected_session,
    )
    return {"ok": True, "result": result}


async def close_tab(app, *, instance: str, tab_id: int,
                    auth_ctx: str | None = None,
                    expected_session: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_CLOSE_TAB, {"tabId": tab_id}, auth_ctx=auth_ctx,
        expected_session=expected_session,
    )
    return {"ok": True, "result": result}


async def focus_tab(app, *, instance: str, tab_id: int,
                    auth_ctx: str | None = None,
                    expected_session: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_FOCUS_TAB, {"tabId": tab_id}, auth_ctx=auth_ctx,
        expected_session=expected_session,
    )
    return {"ok": True, "result": result}


async def move_tab(app, *, instance: str, tab_id: int, window_id: int,
                   index: int | None = None, auth_ctx: str | None = None,
                   expected_session: str | None = None) -> dict:
    """Move one tab to a window/position INSIDE one browser (§6/§9).

    The gap this fills: relocation BETWEEN instances is the §7 open+close pair, which
    only works because the browsers are separate processes. Between the windows of one
    browser the fleet had no verb at all — the agent could open, close, focus and
    navigate a tab, and fold every window into one, but not put a single tab where it
    belongs.

    ``index`` is optional and omitted from the frame when absent, so the extension's
    own default (-1 = append to the end) is the ONE definition of "no position given".

    The extension owns the guards, as it does for every command: the target window
    must pass §9's mergeable predicate (``no_window`` otherwise) and a PINNED tab is
    never moved across a window boundary (``pinned_cross_window``, nothing moved) —
    both surface here as a :class:`ToolError` carrying that code, which is exactly
    what makes the pinned refusal actionable rather than a generic failure.

    No ``actions`` row: this follows its siblings ``open_tab`` / ``close_tab`` /
    ``focus_tab``, which journal nothing from the MCP door either. (``merge_windows``
    does, because §9 requires the manual merge to be recorded and it shares that
    writer with the HTTP button.)
    """
    await _ensure_not_paused(app)
    params: dict = {"tabId": tab_id, "windowId": window_id}
    if index is not None:
        params["index"] = index
    result = await _command(app, instance, protocol.CMD_MOVE_TAB, params, auth_ctx=auth_ctx,
                            expected_session=expected_session)
    return {"ok": True, "result": result}


async def merge_windows(app, *, instance: str, params: dict | None = None,
                        auth_ctx: str | None = None,
                        expected_session: str | None = None) -> dict:
    """Fold an instance's windows into one (§9). Delegates to the SHARED core in
    :mod:`src.api.instances` — the same one ``POST /api/instances/:id/merge_windows``
    runs — so the startpage button and the agent cannot drift apart.

    NO ``force``: the HTTP twin honours ``{"force": true}`` because §9 calls it the
    human's button and §7's exception is for the human's buttons. An agent is not a
    human at the keyboard, and a paused system exists precisely to stop the MCP caller
    (§7/§12) — so this verb is gated unconditionally, ``forced`` is never passed (it
    defaults to False and only affects the archive marker anyway), and a ``force`` key
    smuggled inside ``params`` reaches the extension as a junk param, never the gate:
    :func:`_ensure_not_paused` has already refused by then."""
    await _ensure_not_paused(app)
    try:
        result = await instances_api.merge_windows(
            app, instance, params, initiator="mcp", auth_ctx=auth_ctx,
            expected_session=expected_session,
        )
    except CommandError as exc:
        raise ToolError(exc.code, exc.message)
    return {"ok": True, "result": result, "merged": result["merged"]}


async def execute_js(app, *, instance: str, tab_id: int, code: str,
                     world: str | None = None, url_at_exec: str | None = None,
                     auth_ctx: str | None = None,
                     expected_session: str | None = None) -> dict:
    """Run JS in a tab as an MCP verb. ``send_command`` writes the js_audit row
    BEFORE the send and enforces the runtime kill-switch (§12): a disabled/rejected
    call is still audited (with ``initiator='mcp'`` + the MCP ``auth_ctx``), and the
    extension's own execute_js checkbox still gates it at the edge. Refused while
    paused."""
    await _ensure_not_paused(app)
    params: dict = {"tabId": tab_id, "code": code}
    if world is not None:
        params["world"] = world
    if url_at_exec is not None:
        params["urlAtExec"] = url_at_exec
    result = await _command(app, instance, protocol.CMD_EXECUTE_JS, params, auth_ctx=auth_ctx,
                            expected_session=expected_session)
    return {"ok": True, "result": result}


# --- relocate: phase-A open + a live relocate row (§7 two-phase, §11) --------
def _read_relocate_inputs(instance_from: str, tab_id: int, instance_to: str):
    """Reader ``fn(conn)``: the source tab row + both instances' current sessions.

    Returns ``(tab, session_from, session_to)`` where ``tab`` is the source tabs row
    (or None). The sessions come from the ``instances`` mirror; the caller prefers a
    live ``ConnState`` session when one exists (same order phase A uses)."""
    import sqlite3

    def _fn(conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        tab = conn.execute(
            "SELECT instance_id, tab_id, window_id, url, title, opened_at, "
            "last_active_at, age_unknown FROM tabs WHERE instance_id = ? AND tab_id = ?",
            (instance_from, tab_id),
        ).fetchone()
        sessions = {
            r["id"]: r["session_id"]
            for r in conn.execute(
                "SELECT id, session_id FROM instances WHERE id IN (?, ?)",
                (instance_from, instance_to),
            ).fetchall()
        }
        return tab, sessions.get(instance_from), sessions.get(instance_to)

    return _fn


async def relocate_tab(app, *, instance_from: str, tab_id: int, instance_to: str,
                       auth_ctx: str | None = None,
                       expected_session_from: str | None = None) -> dict:
    """MCP-initiated relocation: do phase A (open the copy in the target) and write a
    live ``relocate`` row (``kind='relocate'``, ``status='done'``, ``initiator='mcp'``)
    so the NEXT curator pass's phase B closes the source under §7's full close guards
    (idle / not-pinned / not-audible / not active-in-focus / survivor still present).

    A one-shot move that closed the source here would skip those guards (§11) — so
    this deliberately does ONLY phase A. The source is untouched until the guarded
    phase B. Reuses the copy-tab writer and the actions writer; no lease/epoch (an
    MCP verb is not a pass), no quarantine strike (that is a pass-internal latch)."""
    await _ensure_not_paused(app)
    db, registry = app.state.db, app.state.ext_registry

    tab, db_session_from, db_session_to = await db.read(
        _read_relocate_inputs(instance_from, tab_id, instance_to)
    )
    if tab is None:
        raise ToolError("no_such_tab", f"no mirrored tab {tab_id} on {instance_from}")

    # Prefer the live ConnState session (freshest), fall back to the mirror — the
    # same precedence phase A uses. These sessions are recorded on the relocate row;
    # the pass only completes it while BOTH still match (mirror.py liveness rule).
    cs_from = registry.get(instance_from)
    cs_to = registry.get(instance_to)
    session_from = cs_from.session_id if cs_from is not None else db_session_from
    session_to = cs_to.session_id if cs_to is not None else db_session_to

    # #47 session epoch — SOURCE side only. Unlike the direct verbs there is no source
    # command to stamp here: relocate does ONLY phase A (the copy opens in the TARGET),
    # and the source ``close_tab`` is the pass's deferred phase B. So the source guard is
    # a pre-check on the source session, BEFORE phase A opens any copy — a mismatch means
    # the source browser has restarted since the agent read the session, its ``tab_id`` is
    # from a dead epoch, and relocating it would copy a stale tab and later close the wrong
    # one. Refuse with ``stale_session`` and touch nothing (no copy, no relocate row).
    #
    # The recorded ``session_id_from`` (below) still carries this epoch onto the relocate
    # row, so if the source flips AFTER this check the pass's own mirror.py liveness rule
    # (``src.session_id == session_id_from``) refuses phase B — the source close is
    # protected end-to-end without touching src/curator/. The TARGET open gets NO expected
    # session on purpose: opening a copy in a restarted target is correct, not dangerous.
    if expected_session_from is not None and expected_session_from != session_from:
        raise ToolError(
            protocol.ERR_STALE_SESSION,
            f"source session for {instance_from} is not {expected_session_from!r} "
            "(the source browser restarted); relocation refused",
        )

    now = _now_ms()
    url = tab["url"]
    url_norm = normalize_url(url)
    seed_age_ms = now - tab["last_active_at"]
    seed_opened_ago_ms = now - tab["opened_at"]

    # Phase A: open the copy in the target (async, outside any txn).
    result = await _command(
        app, instance_to, protocol.CMD_OPEN_TAB,
        {
            "url": url, "pinned": False, "active": False,
            "seed_age_ms": seed_age_ms, "seed_opened_ago_ms": seed_opened_ago_ms,
            "seed_age_unknown": bool(tab["age_unknown"]),
        },
        auth_ctx=auth_ctx,
    )
    tab_id_to = result.get("tabId")
    window_id_to = result.get("windowId")
    # Same guard as phase A (SUGGESTION 6): never write a relocate row pointing at a
    # non-int copy id — a live row aimed at a phantom copy. Better no row at all.
    if not isinstance(tab_id_to, int) or isinstance(tab_id_to, bool):
        raise ToolError(
            "open_failed", f"open_tab returned no integer tabId ({tab_id_to!r})"
        )

    # The seed clocks the copy inherits from the source (so it is not "younger" for
    # dedup/idle/singleton), mirroring phase A's copy-tab row.
    seed = SimpleNamespace(
        url=url, title=tab["title"], opened_at=tab["opened_at"],
        last_active_at=tab["last_active_at"], age_unknown=tab["age_unknown"],
    )

    def _write(conn):
        # Reuse phase A's copy-tab UPSERT so the copy's mirror row is identical.
        from src.curator.phases import _insert_copy_tab

        _insert_copy_tab(
            conn, instance_id=instance_to, tab_id=tab_id_to, window_id=window_id_to,
            tab=seed, now=now,
        )
        return insert_action(
            conn, ts=now, kind="relocate", status="done", initiator="mcp",
            instance_from=instance_from, instance_to=instance_to,
            tab_id=tab["tab_id"], session_id_from=session_from,
            tab_id_to=tab_id_to, session_id_to=session_to,
            decision="mcp_relocate",
            src_opened_at=tab["opened_at"], src_last_active_at=tab["last_active_at"],
            src_age_unknown=tab["age_unknown"],
            url=url, url_norm=url_norm, title=tab["title"], pinned=0,
        )

    action_id = await db.write(_write)
    return {
        "ok": True, "action_id": action_id, "tab_id_to": tab_id_to,
        "instance_to": instance_to,
    }


# --- pass + pause ------------------------------------------------------------
async def run_pass(app, *, dry_run: bool = False) -> dict:
    """Trigger one curator pass (§7). Delegates to the runner, which itself honours a
    pause at step 1 (and never mutes a dry_run — the plan is exactly why one pauses)."""
    app_state = app.state
    return await runner.run_pass(
        app_state.db, app_state.ext_registry, app_state.settings,
        dry_run=bool(dry_run),
        clock_guard=getattr(app_state, "curator_clock", None),
    )


async def pause(app, *, minutes: int | None = None) -> dict:
    """Pause the curator: write ``pause_until`` + bump the fencing epoch (stops an
    in-flight pass). Not itself a "mutating verb" that pause refuses. Defaults to
    ``PAUSE_DEFAULT_MIN`` and is clamped to a finite window (a pause is never
    infinite, §7) — the same write-shape as ``POST /api/pause``."""
    mins = pause_ops.clamp_minutes(minutes, app.state.settings.pause_default_min)
    now = _now_ms()
    until = await app.state.db.write(lambda c: pause_ops.pause(c, now=now, minutes=mins))
    return {"ok": True, "paused_until": until}


async def resume(app) -> dict:
    """Resume the curator (§7/§12): apply the TTL shift on the ACTUAL pause duration,
    clear ``pause_until`` / ``pause_started_at`` / ``resume_pending``, THEN run a pass
    immediately — the SAME :func:`src.api.pause.resume_now` core ``DELETE /api/pause``
    runs. §7 makes the immediate pass part of what "resume by hand" MEANS (only a
    timeout expiry defers behind a click); stopping after the settings write left the
    same verb with two behaviours depending on which door it was called through, and
    an agent's resume left the curator idle until the next tick.

    KNOWN COST, accepted deliberately: this now BLOCKS for the whole pass — tens of
    seconds on a large fleet — where it used to be one settings write. Detaching the
    pass and answering immediately was considered and rejected, because it would restore
    exactly the divergence just removed: ``DELETE /api/pause`` returns the pass RESULT
    (the startpage shows what the resume did), so a fire-and-forget MCP twin would again
    be the same verb with two meanings. If the wait becomes a real problem it must be
    changed on BOTH doors at once — and the pass is idempotent under the lease, so a
    client that times out has not lost anything: the pass runs to completion regardless
    and its outcome is readable from ``passes`` / ``list_actions``."""
    outcome = await pause_api.resume_now(app)
    return {"ok": True, **outcome}
