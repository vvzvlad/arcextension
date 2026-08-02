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
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_ext_token, require_not_paused, require_operational
from src.api.restore import _is_fresh, _request_snapshot
from src.rules import access
from src.rules.matcher import InvalidPattern, compile_pattern
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


async def _ensure_fresh_for_preview(registry, db, instance_id: str, settings):
    """Actively request a fresh snapshot from ONE instance for preview (§8).

    Reuses restore's ``_request_snapshot`` + poll-until-fresh mechanism, but where
    restore 409s an unrefreshable source, preview marks it **"not counted"** with a
    reason and carries on (§8: "неответивший инстанс отмечается «не учтён»"). Returns
    ``(counted, reason)`` — ``fresh`` (answered / already fresh), ``disconnected``
    (no live socket, or the socket was superseded mid-poll), or ``timeout``
    (connected but did not answer within ``SNAPSHOT_TIMEOUT_MS``).
    """
    conn_state = registry.get(instance_id)
    if conn_state is None:
        return (False, "disconnected")
    if await _is_fresh(db, instance_id, conn_state, settings):
        return (True, "fresh")
    # Connected but stale: ask for a fresh snapshot and poll until it lands. Same
    # 50ms cadence / fresh-reader-per-tick as restore._ensure_fresh.
    await _request_snapshot(conn_state)
    deadline = time.monotonic() + settings.snapshot_timeout_ms / 1000.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        if registry.get(instance_id) is not conn_state:
            return (False, "disconnected")  # socket superseded/dropped underneath us
        if await _is_fresh(db, instance_id, conn_state, settings):
            return (True, "fresh")
    return (False, "timeout")


async def _refresh_for_preview(app, settings) -> dict:
    """Freshen EVERY known instance and return {instance_id: (counted, reason)}.

    Without this, preview would count against a stale mirror: snapshots refresh only
    on a pass (~5 min) or tick (~60s), so an interactive popup preview would read a
    stale mirror, report `relocations/closures = 0`, and the confirm gate — the §8
    replacement for the removed action-count limiter (стр.15) — would never fire.

    Registry (the live sockets) is the connectivity source of truth, so a row that
    is ``connected=1`` in the mirror but has no live socket is correctly
    ``disconnected`` here. This is async WS I/O and runs OUTSIDE any DB transaction.
    """
    registry = app.state.ext_registry
    db = app.state.db
    known = await db.read(access.known_instance_ids)
    return {
        iid: await _ensure_fresh_for_preview(registry, db, iid, settings)
        for iid in sorted(known)
    }


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


# --- POST /api/rules --------------------------------------------------------
async def create_rule(request: Request) -> JSONResponse:
    require_ext_token(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused — a rule edit mutates (§7)
    body = await _body(request)
    fields = _extract_rule_fields(body)
    _validate_pattern_or_422(fields["pattern"])
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
        rule_id = body.get("id")
        candidate = _build_candidate(current, op, fields, rule_id)

    res = await _run_preview(request, candidate)
    payload = res.to_dict()
    payload["requires_confirm"] = (
        _requires_confirm(current, candidate, res) if op != "delete" else True
    )
    payload["not_counted"] = _not_counted(res)
    return JSONResponse(payload)


# --- POST /api/rules/:id/reset ----------------------------------------------
async def reset_rule(request: Request) -> JSONResponse:
    """Record/return the manual ``reset`` intent (§8, §10): apply ``canonical_url``.

    The actual tab-content change is a command in a later phase; here we only
    confirm the rule exists and return the reset target, keeping it minimal."""
    require_ext_token(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused (§7)
    rule_id = request.path_params["rule_id"]
    row = await request.app.state.db.read(lambda c: access.get_rule(c, rule_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
    return JSONResponse(
        {
            "ok": True,
            "id": rule_id,
            "canonical_url": row["canonical_url"],
            "note": "reset intent recorded; the tab-content change is a later phase (§10)",
        }
    )


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
