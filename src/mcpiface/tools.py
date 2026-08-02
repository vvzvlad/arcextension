"""MCP tool handlers (§11) — thin adapters over already-built logic.

Every handler is a plain ``async def`` taking the HOST app (for ``app.state.db`` /
``ext_registry`` / ``settings``) plus its tool arguments, so each can be unit-tested
directly without an MCP client or a socket. NOTHING here reimplements state, rules,
command, pass or pause logic — the handlers only call the reused functions:

* reads     -> :mod:`src.db.state`, :mod:`src.rules.access`, :mod:`src.api.actions`
* freshness -> :func:`src.api.state.kick_state_refresh` (same path as ``/api/state``)
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

import time
from types import SimpleNamespace

from starlette.exceptions import HTTPException

from src.api import actions as actions_api
from src.api import rules as rules_api
from src.api.state import kick_state_refresh
from src.curator import pause as pause_ops
from src.curator import runner
from src.db import state as state_read
from src.db.actions import insert_action, normalize_url
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
async def list_instances(app) -> dict:
    """Instances mirror + per-instance ``snapshot_at`` + ``paused_until`` (§11).

    Goes through the SAME freshness path as ``/api/state`` (kick a single-flight
    refresh, then return the current mirror) so a stale mirror is at least refreshed
    for the next call, and the agent sees each instance's ``snapshot_at`` age. The
    ``paused_until`` field lets the agent tell a pause from a broken curator (§11)."""
    db, settings = app.state.db, app.state.settings
    await kick_state_refresh(app, db, settings)
    instances = await db.read(state_read._read_instances)
    paused_until = await db.read(pause_ops.read_pause_until)
    return {
        "server_now": _now_ms(),
        "paused_until": paused_until,
        "instances": instances,
    }


async def list_tabs(app) -> dict:
    """Tabs mirror + per-instance ``snapshot_at`` (§11).

    Same freshness path as ``/api/state``; returns ``snapshot_at`` per instance so
    the agent never acts on a mirror of unknown age (§11: else it re-opens a tab a
    human opened three minutes ago — permanently, in ``main`` where there is no
    dedup)."""
    db, settings = app.state.db, app.state.settings
    await kick_state_refresh(app, db, settings)
    tabs = await db.read(state_read._read_tabs)
    instances = await db.read(state_read._read_instances)
    return {
        "server_now": _now_ms(),
        "tabs": tabs,
        # snapshot_at per instance — the age the agent must weigh before acting.
        "snapshot_at": {i["id"]: i["snapshot_at"] for i in instances},
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
    """Return a rule's reset target (§8/§10): the ``canonical_url`` to apply. Reuses
    the ``/api/rules/:id/reset`` logic — the tab-content change is a later phase."""
    await _ensure_not_paused(app)
    row = await app.state.db.read(lambda c: rules_access.get_rule(c, rule_id))
    if row is None:
        raise ToolError("not_found", f"rule {rule_id} not found")
    return {
        "ok": True,
        "id": rule_id,
        "canonical_url": row["canonical_url"],
        "note": "reset intent recorded; the tab-content change is a later phase (§10)",
    }


# --- commands (initiator='mcp' + auth_ctx, §12) ------------------------------
async def _command(app, instance, command, params, *, auth_ctx):
    """Issue one extension command as an MCP verb. Every command carries
    ``initiator='mcp'`` and ``auth_ctx`` = the MCP session (§12: js_audit records the
    session, never a token id). Command failures are surfaced as a ``ToolError`` so
    the agent sees the §6 error code instead of a transport-level fault."""
    settings = app.state.settings
    try:
        return await send_command(
            app.state.ext_registry, app.state.db, instance, command, params,
            cmd_timeout_ms=settings.cmd_timeout_ms, initiator="mcp", auth_ctx=auth_ctx,
        )
    except CommandError as exc:
        raise ToolError(exc.code, exc.message)


async def open_tab(app, *, instance: str, url: str, pinned: bool = False,
                   active: bool = False, auth_ctx: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_OPEN_TAB,
        {"url": url, "pinned": bool(pinned), "active": bool(active)}, auth_ctx=auth_ctx,
    )
    return {"ok": True, "result": result}


async def close_tab(app, *, instance: str, tab_id: int,
                    auth_ctx: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_CLOSE_TAB, {"tabId": tab_id}, auth_ctx=auth_ctx
    )
    return {"ok": True, "result": result}


async def focus_tab(app, *, instance: str, tab_id: int,
                    auth_ctx: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_FOCUS_TAB, {"tabId": tab_id}, auth_ctx=auth_ctx
    )
    return {"ok": True, "result": result}


async def merge_windows(app, *, instance: str, params: dict | None = None,
                        auth_ctx: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_MERGE_WINDOWS, params or {}, auth_ctx=auth_ctx
    )
    return {"ok": True, "result": result}


async def execute_js(app, *, instance: str, tab_id: int, code: str,
                     world: str | None = None, url_at_exec: str | None = None,
                     auth_ctx: str | None = None) -> dict:
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
    result = await _command(app, instance, protocol.CMD_EXECUTE_JS, params, auth_ctx=auth_ctx)
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
                       auth_ctx: str | None = None) -> dict:
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
    then clear ``pause_until`` / ``pause_started_at`` / ``resume_pending``. Same
    ``pause.resume`` write-shape as ``DELETE /api/pause``."""
    now = _now_ms()
    await app.state.db.write(lambda c: pause_ops.resume(c, now=now))
    return {"ok": True}
