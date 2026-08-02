"""``/api/rules`` CRUD + ``/api/rules/preview`` + ``/api/rules/:id/reset`` (§8, §10).

Every route is Bearer-authed (``EXT_TOKEN``) and refuses degraded mode. The
mutating routes (POST/PUT/DELETE) run the SAME server-side preview and require
``confirm_impact`` when the change is momentous (§8):

* ``relocations + closures > 0`` — the threshold is the SUM, not just closures: the
  pass after a rule edit does only phase-A opens, so ``closures`` is 0 then, but
  every phase-A open is a phase-B close obligation (§8 ⚠️).
* crossing the empty boundary in EITHER direction — creating the first rule enables
  the "unruled → main" drain for all themed instances (the largest burst), and
  deleting the last rule disables curation entirely (§8). Both are gated even if the
  modeled next-pass count happens to be 0.

Missing ``confirm_impact`` when confirmation is required => 409 carrying the preview
payload, so the client can show it and re-submit with the flag.

``GET /api/rules/:id`` returns the SAME rule object the list puts in its array (§10's
``GET/POST/PUT/DELETE /api/rules[/:id]``).

``POST /api/rules/:id/reset`` is the manual ``reset`` (§8, §10) and the ONE place in
the system that changes a tab's content: it navigates the rule's SURVIVING tab to the
rule's ``canonical_url`` via the ``navigate_tab`` command and journals an
``actions(kind='reset')`` row. §8 is explicit that a pass never applies
``canonical_url``. :func:`perform_reset` is the shared core the MCP ``reset_singleton``
tool runs too, so HTTP and MCP have identical semantics and side effects (§11).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.freshness import ensure_fresh
from src.api.guards import require_ext_token, require_not_paused, require_operational
from src.curator.decide import survivor_key
from src.db.actions import insert_action, normalize_url
from src.ext import protocol
from src.ext.commands import CommandError, send_command
from src.rules import access
from src.rules.matcher import (
    InvalidPattern,
    best_match_compiled,
    compile_pattern,
    compile_rules,
    normalize_target,
)
from src.rules.preview import (
    has_active_rules,
    load_preview_input,
    simulate,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rule_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "pattern": row["pattern"],
        "instance_id": row["instance_id"],
        "singleton": row["singleton"],
        "canonical_url": row["canonical_url"],
        "note": row["note"],
        "invalid": row["invalid"],
        "created_at": row["created_at"],
    }


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return data


def _extract_rule_fields(body: dict) -> dict:
    """Pull the rule fields from a body, defaulting the optional ones. A body may
    nest them under ``rule`` or carry them flat (the §10 preview shape
    ``{pattern, instance_id, singleton}``)."""
    src = body.get("rule") if isinstance(body.get("rule"), dict) else body
    return {
        "pattern": src.get("pattern"),
        "instance_id": src.get("instance_id"),
        "singleton": bool(src.get("singleton", False)),
        "canonical_url": src.get("canonical_url"),
        "note": src.get("note"),
    }


def _build_candidate(current: list[dict], op: str, fields: dict, rule_id) -> list[dict]:
    """The rule set AFTER the candidate change (§8: model the whole next pass)."""
    if op == "delete":
        return [r for r in current if r["id"] != rule_id]

    new_id = rule_id
    if op == "create":
        new_id = max((r["id"] for r in current), default=0) + 1  # loses id ties (§8c)
    candidate_rule = {
        "id": new_id,
        "pattern": fields["pattern"],
        "instance_id": fields["instance_id"],
        "singleton": 1 if fields["singleton"] else 0,
        "canonical_url": fields["canonical_url"],
        "note": fields["note"],
        "invalid": 0,
    }
    if op == "create":
        return current + [candidate_rule]
    # update: replace in place, preserving order
    return [candidate_rule if r["id"] == rule_id else r for r in current]


# Preview's per-instance wait budget: ONE snapshot timeout, not the 3× worst case
# ``ensure_fresh`` may spend for restore. §8 gives preview the escape restore does not
# have — «неответивший инстанс отмечается "не учтён"» — and it needs it: a rule edit runs
# the preview synchronously, DELETE runs it TWICE (the always-on 409 plus the confirmed
# retry), and a couple of wedged instances at 3×30 s would turn one button into a
# reverse-proxy 504 rather than a slow page.
_PREVIEW_BUDGET_FACTOR = 1


async def _ensure_fresh_for_preview(registry, db, instance_id: str, settings):
    """Actively request a fresh snapshot from ONE instance for preview (§8).

    Uses the SHARED :func:`src.api.freshness.ensure_fresh` — so a preview opened while
    a curator pass is collecting snapshots WAITS for the pass's snapshot instead of
    overwriting its ``pending_snapshot_id`` and ejecting the instance from that pass.
    Where restore 409s an unrefreshable source, preview marks it **"not counted"** with
    a reason and carries on (§8: "неответивший инстанс отмечается «не учтён»"). Returns
    ``(counted, reason)`` — ``fresh`` (answered / already fresh), ``disconnected``
    (no live socket, or the socket was superseded mid-poll), or ``timeout``
    (connected but the mirror did not become fresh within ``SNAPSHOT_TIMEOUT_MS``).
    """
    fresh, reason, _conn_state = await ensure_fresh(
        registry, db, instance_id, settings,
        budget_ms=settings.snapshot_timeout_ms * _PREVIEW_BUDGET_FACTOR,
    )
    return (fresh, reason)


async def _refresh_for_preview(app, settings) -> dict:
    """Freshen EVERY known instance and return {instance_id: (counted, reason)}.

    Without this, preview would count against a stale mirror: snapshots refresh only
    on a pass (~5 min) or tick (~60s), so an interactive popup preview would read a
    stale mirror, report `relocations/closures = 0`, and the confirm gate — the §8
    replacement for the removed action-count limiter (стр.15) — would never fire.

    Registry (the live sockets) is the connectivity source of truth, so a row that
    is ``connected=1`` in the mirror but has no live socket is correctly
    ``disconnected`` here. This is async WS I/O and runs OUTSIDE any DB transaction.

    Fanned out CONCURRENTLY. As a sequential loop the waits ADDED UP — N wedged
    instances cost N budgets, so the wall time of an interactive rule edit grew with the
    size of the fleet, which is exactly backwards. Each call only awaits its own socket
    and polls its own reader connection, so there is nothing to serialise.
    """
    registry = app.state.ext_registry
    db = app.state.db
    known = sorted(await db.read(access.known_instance_ids))
    results = await asyncio.gather(
        *(_ensure_fresh_for_preview(registry, db, iid, settings) for iid in known)
    )
    return dict(zip(known, results))


async def _run_preview(request: Request, candidate: list[dict]):
    app = request.app
    settings = app.state.settings
    override = await _refresh_for_preview(app, settings)
    inp = await app.state.db.read(
        lambda c: load_preview_input(
            c,
            candidate,
            now=_now_ms(),
            idle_ms=settings.idle_minutes * 60_000,
            state_fresh_ms=settings.state_fresh_ms,
            main_instance_id=settings.main_instance_id,
            counted_override=override,
        )
    )
    return simulate(inp)


def _not_counted(res) -> list[dict]:
    """The instances preview could not count against a fresh mirror (§8)."""
    return [
        {"id": i["id"], "reason": i["reason"]}
        for i in res.instances
        if not i["counted"]
    ]


def _requires_confirm(before: list[dict], candidate: list[dict], res) -> bool:
    # SUM threshold (§8 ⚠️) OR crossing the empty boundary either way (§8) OR ANY
    # instance we could not count. The last is the §8 danger: a disconnected/stale
    # instance is excluded from `impact`, so a large burst hiding behind an
    # uncountable instance would otherwise slip through WITHOUT confirmation.
    if res.impact > 0:
        return True
    if has_active_rules(before) != has_active_rules(candidate):
        return True
    return bool(_not_counted(res))


def _validate_pattern_or_422(pattern) -> None:
    try:
        compile_pattern(pattern) if isinstance(pattern, str) else _raise_not_str()
    except InvalidPattern as e:
        raise HTTPException(status_code=422, detail=f"invalid rule pattern: {e}")


def _raise_not_str():
    raise InvalidPattern("pattern must be a string")


def _validate_canonical_or_422(canonical_url) -> None:
    """A rule's ``canonical_url`` must be an http(s) URL with a host, or absent.

    Validated at SAVE, not only at the edge. §12 wants the extension to check at the edge
    «а не только» as rule validation, and since the manual ``reset`` now really sends this
    value in a ``navigate_tab``, an unvalidated one fails at the far end of the chain:
    the extension's ``isHttpUrl`` rejects it, the human gets a 409 with a bare
    ``precondition_failed`` and an ``actions`` row saying ``failed``, and nothing anywhere
    says "the URL you typed into the rule is not a URL". A 422 at save costs one call and
    names the field.
    """
    if canonical_url in (None, ""):
        return  # optional — a rule without a canonical target simply has no reset
    if not isinstance(canonical_url, str) or normalize_target(canonical_url) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"canonical_url {canonical_url!r} is not an http(s) URL with a host; "
                "the manual reset navigates a tab to it, and the extension refuses "
                "anything else at the edge (§12)"
            ),
        )


async def _validate_instance_or_422(request: Request, instance_id) -> None:
    if not instance_id or not isinstance(instance_id, str):
        raise HTTPException(status_code=422, detail="rule instance_id is required")
    known = await request.app.state.db.read(access.known_instance_ids)
    main = request.app.state.settings.main_instance_id
    # Reject a typo at write (§12): an unknown target is a permanent invisible
    # `deferred` otherwise. The configured main instance is allowed even before it
    # has ever connected (it may have no row yet).
    if instance_id not in known and instance_id != main:
        raise HTTPException(
            status_code=422, detail=f"unknown instance_id {instance_id!r}"
        )


# --- GET /api/rules ---------------------------------------------------------
async def list_rules(request: Request) -> JSONResponse:
    require_ext_token(request)
    require_operational(request)
    rows = await request.app.state.db.read(access.list_rules)
    return JSONResponse({"rules": [_rule_to_dict(r) for r in rows]})


# --- GET /api/rules/:id -----------------------------------------------------
async def get_rule(request: Request) -> JSONResponse:
    """One rule by id (§10's ``GET/POST/PUT/DELETE /api/rules[/:id]``).

    The body is EXACTLY the object ``GET /api/rules`` puts in its ``rules`` array —
    same mapper, so an editor that fetched the list and one that fetched a single rule
    can never see two shapes. 404 when there is no such rule."""
    require_ext_token(request)
    require_operational(request)
    rule_id = request.path_params["rule_id"]
    row = await request.app.state.db.read(lambda c: access.get_rule(c, rule_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
    return JSONResponse(_rule_to_dict(row))


# --- POST /api/rules --------------------------------------------------------
async def create_rule(request: Request) -> JSONResponse:
    require_ext_token(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused — a rule edit mutates (§7)
    body = await _body(request)
    fields = _extract_rule_fields(body)
    _validate_pattern_or_422(fields["pattern"])
    _validate_canonical_or_422(fields["canonical_url"])
    await _validate_instance_or_422(request, fields["instance_id"])

    current = await _current_rules(request)
    candidate = _build_candidate(current, "create", fields, None)
    res = await _run_preview(request, candidate)
    if _requires_confirm(current, candidate, res) and not body.get("confirm_impact"):
        return _confirm_needed(res)

    now = _now_ms()
    rule_id = await request.app.state.db.write(
        lambda c: access.insert_rule(
            c,
            pattern=fields["pattern"],
            instance_id=fields["instance_id"],
            singleton=fields["singleton"],
            canonical_url=fields["canonical_url"],
            note=fields["note"],
            created_at=now,
        )
    )
    return JSONResponse({"ok": True, "id": rule_id, "preview": res.to_dict()}, status_code=201)


# --- PUT /api/rules/:id -----------------------------------------------------
async def update_rule(request: Request) -> JSONResponse:
    require_ext_token(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused (§7)
    rule_id = request.path_params["rule_id"]
    body = await _body(request)
    fields = _extract_rule_fields(body)
    _validate_pattern_or_422(fields["pattern"])
    _validate_canonical_or_422(fields["canonical_url"])
    await _validate_instance_or_422(request, fields["instance_id"])

    current = await _current_rules(request)
    if not any(r["id"] == rule_id for r in current):
        raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
    candidate = _build_candidate(current, "update", fields, rule_id)
    res = await _run_preview(request, candidate)
    if _requires_confirm(current, candidate, res) and not body.get("confirm_impact"):
        return _confirm_needed(res)

    await request.app.state.db.write(
        lambda c: access.update_rule(
            c,
            rule_id,
            pattern=fields["pattern"],
            instance_id=fields["instance_id"],
            singleton=fields["singleton"],
            canonical_url=fields["canonical_url"],
            note=fields["note"],
        )
    )
    return JSONResponse({"ok": True, "id": rule_id, "preview": res.to_dict()})


# --- DELETE /api/rules/:id --------------------------------------------------
async def delete_rule(request: Request) -> JSONResponse:
    require_ext_token(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused (§7)
    rule_id = request.path_params["rule_id"]
    body = {}
    if await request.body():
        body = await _body(request)

    current = await _current_rules(request)
    if not any(r["id"] == rule_id for r in current):
        raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
    candidate = _build_candidate(current, "delete", {}, rule_id)
    res = await _run_preview(request, candidate)
    # DELETE is ALWAYS gated (§8): draining a rule's tabs to main, or disabling
    # curation by removing the last rule, are both large and both silent today.
    if not body.get("confirm_impact"):
        return _confirm_needed(res)

    await request.app.state.db.write(lambda c: access.delete_rule(c, rule_id))
    return JSONResponse({"ok": True, "id": rule_id, "preview": res.to_dict()})


# --- POST /api/rules/preview ------------------------------------------------
async def preview_rule(request: Request) -> JSONResponse:
    require_ext_token(request)
    require_operational(request)
    body = await _body(request)
    op = body.get("op", "create")
    if op not in ("create", "update", "delete"):
        raise HTTPException(status_code=400, detail=f"unknown op {op!r}")

    current = await _current_rules(request)
    if op == "delete":
        rule_id = body.get("id")
        candidate = _build_candidate(current, "delete", {}, rule_id)
    else:
        fields = _extract_rule_fields(body)
        _validate_pattern_or_422(fields["pattern"])
        _validate_canonical_or_422(fields["canonical_url"])
        rule_id = body.get("id")
        candidate = _build_candidate(current, op, fields, rule_id)

    res = await _run_preview(request, candidate)
    payload = res.to_dict()
    payload["requires_confirm"] = (
        _requires_confirm(current, candidate, res) if op != "delete" else True
    )
    payload["not_counted"] = _not_counted(res)
    return JSONResponse(payload)


# --- the manual `reset` (§8, §10) -------------------------------------------
def _read_rule_tabs(conn: sqlite3.Connection, instance_id: str) -> list[SimpleNamespace]:
    """The home instance's mirrored tabs as attribute objects.

    Attribute access (not ``sqlite3.Row``) because :func:`src.curator.decide.survivor_key`
    — the ONE §8 ladder, reused rather than re-derived — reads ``tab.last_active_at``
    &co. off a mirror row object.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT instance_id, tab_id, url, title, opened_at, last_active_at, age_unknown "
        "FROM tabs WHERE instance_id = ?",
        (instance_id,),
    ).fetchall()
    return [
        SimpleNamespace(
            instance_id=r["instance_id"],
            tab_id=r["tab_id"],
            url=r["url"],
            title=r["title"],
            opened_at=r["opened_at"],
            last_active_at=r["last_active_at"],
            age_unknown=r["age_unknown"],
        )
        for r in rows
    ]


def _pick_reset_target(rule, all_rules, tabs):
    """The SURVIVING tab of ``rule`` in its home instance, or ``None``.

    "Surviving" is the §8 ladder used everywhere else
    (:func:`src.curator.decide.survivor_key`): max ``last_active_at``, tie → min
    ``opened_at``, tie → min ``tab_id``, and an ``age_unknown`` row loses to any
    observed one — the SAME tab a ``singleton_close`` would have kept, which is the
    only sensible target for "put the app back on its canonical page".

    A tab belongs to ``rule`` only when ``rule`` is its BEST match across the whole
    rule set (:func:`src.rules.matcher.best_match_compiled`) — never merely "the
    pattern matches". Otherwise a broad rule would reset a tab that a more specific
    rule owns, which is not the tab the pass keeps either.
    """
    compiled = compile_rules(all_rules)
    rule_id = rule["id"]
    owned = [t for t in tabs if _best_rule_id(t.url, compiled) == rule_id]
    if not owned:
        return None
    return sorted(owned, key=survivor_key)[0]


def _best_rule_id(url, compiled):
    best = best_match_compiled(url, compiled)
    if best is None:
        return None
    try:
        return best["id"]
    except (KeyError, IndexError, TypeError):
        return getattr(best, "id", None)


def _record_reset(
    conn: sqlite3.Connection, *, rule, tab, url: str, initiator: str, now: int,
    session_id, status: str, reason: str | None,
) -> int:
    """One ``actions(kind='reset')`` row — the archive side of a manual reset (§8/§10).

    ``kind='reset'`` has been in ``ALLOWED_KINDS`` since Фаза 4 and was never written
    by anything; this is its writer. ``instance_from``/``tab_id`` name the tab that was
    navigated, ``url`` is the ``canonical_url`` it was pointed at, ``detail`` keeps the
    tab's previous address so the archive answers "what did this replace".
    """
    return insert_action(
        conn,
        ts=now,
        kind="reset",
        status=status,
        initiator=initiator,
        instance_from=rule["instance_id"],
        instance_to=rule["instance_id"],
        tab_id=tab.tab_id,
        session_id_from=session_id,
        rule_id=rule["id"],
        rule_pattern=rule["pattern"],
        decision="reset",
        url=url,
        url_norm=normalize_url(url),
        title=tab.title,
        reason=reason,
        detail=tab.url,
    )


# The §6 error codes that mean "the client's picture is stale / the edge refused",
# answered with 409 + refetch instead of a transport-class 502.
_RESET_CLIENT_ERRORS = frozenset(
    {protocol.ERR_NO_SUCH_TAB, protocol.ERR_PRECONDITION_FAILED, protocol.ERR_STALE_SESSION}
)


async def _rules_for_matching(db) -> list[dict]:
    return [_rule_to_dict(r) for r in await db.read(access.list_rules)]


async def perform_reset(app, rule_id, *, initiator: str) -> dict:
    """Apply a rule's ``canonical_url`` to its surviving tab — THE manual reset (§8).

    §8 is explicit that ``canonical_url`` is applied **only** by the manual ``reset``
    and that a pass never changes tab content, so this is the one place the
    ``navigate_tab`` command (§6) is issued from the server. Shared verbatim by
    ``POST /api/rules/:id/reset`` and the MCP ``reset_singleton`` tool so both have the
    same semantics and the same side effects (§11 parity); only ``initiator`` differs
    (``'user'`` vs ``'mcp'``), which is what the archive row records.

    Guards are the ones every command path uses (:mod:`src.ext.commands`): the home
    instance must have a live socket AND a fresh mirror (§6 out-of-pass, via the shared
    :mod:`src.api.freshness`), else 409 — a reset decided on a stale mirror would
    navigate the wrong tab. A rule with NO matching tab is **not** an error: there is
    simply nothing to reset (``{"ok": true, "reset": false, "reason": "no_tabs"}``).

    Returns ``{ok, id, reset, reason, canonical_url, instance, tab_id, action_id}``.
    Raises ``HTTPException`` 404 (no such rule), 422 (rule has no ``canonical_url``),
    409 (instance unreachable / mirror not fresh / the extension refused) or 502.
    """
    db = app.state.db
    registry = app.state.ext_registry
    settings = app.state.settings

    rule = await db.read(lambda c: access.get_rule(c, rule_id))
    if rule is None:
        raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
    canonical_url = rule["canonical_url"]
    if not canonical_url:
        raise HTTPException(
            status_code=422,
            detail=f"rule {rule_id} has no canonical_url; nothing to reset to (§8)",
        )

    instance_id = rule["instance_id"]
    # Freshness FIRST (§6): the survivor is chosen from the mirror, so a stale mirror
    # would pick — and navigate — a tab that no longer exists or is no longer the one.
    fresh, reason, conn_state = await ensure_fresh(registry, db, instance_id, settings)
    if not fresh:
        raise HTTPException(
            status_code=409,
            detail=(
                f"instance {instance_id!r} is not usable for reset ({reason}); "
                "refusing to navigate against a stale mirror"
            ),
        )

    all_rules = await _rules_for_matching(db)
    tabs = await db.read(lambda c: _read_rule_tabs(c, instance_id))
    target = _pick_reset_target(rule, all_rules, tabs)
    if target is None:
        # Not an error (§8): a rule that currently holds no tab has nothing to reset.
        return {
            "ok": True, "id": rule_id, "reset": False, "reason": "no_tabs",
            "canonical_url": canonical_url, "instance": instance_id,
            "tab_id": None, "action_id": None,
        }

    now = _now_ms()
    session_id = conn_state.session_id
    try:
        await send_command(
            registry,
            db,
            instance_id,
            protocol.CMD_NAVIGATE_TAB,
            {"tabId": target.tab_id, "url": canonical_url},
            cmd_timeout_ms=settings.cmd_timeout_ms,
            initiator=initiator,
        )
    except CommandError as exc:
        # A refused reset is still journaled — the archive must not lose the attempt.
        await db.write(
            lambda c: _record_reset(
                c, rule=rule, tab=target, url=canonical_url, initiator=initiator,
                now=now, session_id=session_id, status="failed", reason=exc.code,
            )
        )
        # no_such_tab / precondition_failed are "your picture is stale / the edge
        # refused" — the client re-fetches and re-renders (same shape as /api/focus);
        # anything else is a transport-class failure.
        status_code = 409 if exc.code in _RESET_CLIENT_ERRORS else 502
        raise HTTPException(
            status_code=status_code,
            detail={"error": exc.code, "message": exc.message, "refetch": True},
        )

    action_id = await db.write(
        lambda c: _record_reset(
            c, rule=rule, tab=target, url=canonical_url, initiator=initiator,
            now=now, session_id=session_id, status="done", reason=None,
        )
    )
    return {
        "ok": True, "id": rule_id, "reset": True, "reason": None,
        "canonical_url": canonical_url, "instance": instance_id,
        "tab_id": target.tab_id, "action_id": action_id,
    }


# --- POST /api/rules/:id/reset ----------------------------------------------
async def reset_rule(request: Request) -> JSONResponse:
    """Apply the rule's ``canonical_url`` to its surviving tab (§8, §10).

    The manual ``reset`` is the ONLY thing in the system that changes a tab's content
    (§8: «`canonical_url` применяется только ручным действием `reset`; проход
    содержимое вкладок не меняет»). Delegates to :func:`perform_reset` — the same core
    the MCP ``reset_singleton`` tool runs, so the two cannot drift."""
    require_ext_token(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused (§7)
    rule_id = request.path_params["rule_id"]
    return JSONResponse(await perform_reset(request.app, rule_id, initiator="user"))


# --- shared helpers ---------------------------------------------------------
async def _current_rules(request: Request) -> list[dict]:
    rows = await request.app.state.db.read(access.list_rules)
    return [_rule_to_dict(r) for r in rows]


def _confirm_needed(res) -> JSONResponse:
    payload = res.to_dict()
    payload["requires_confirm"] = True
    # Surface WHICH instances were not counted so the human sees why confirmation is
    # required — a large edit can be gated purely because a burst hides behind an
    # uncountable instance (§8).
    payload["not_counted"] = _not_counted(res)
    payload["error"] = "confirm_impact required"
    return JSONResponse(payload, status_code=409)
