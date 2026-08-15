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
* relocate  -> BOTH phases synchronously (#48): open the copy, then close the source
  under the step-4 guards; degrades to today's phase-A-only ``half`` when the source
  close cannot be completed (the pass's phase B / reconcile finishes it later)
* run_pass  -> :func:`src.curator.runner.run_pass`
* pause     -> :mod:`src.curator.pause` (settings write + ``lease.bump_epoch``)

§12: a stopped system REFUSES mutating verbs. Reads and ``pause`` / ``resume`` are
never gated; ``run_pass`` handles the stop itself (a dry_run is never muted — looking
at the plan is exactly why the stop is pressed).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

from starlette.exceptions import HTTPException

from src.api import actions as actions_api
from src.api import exemptions as exemptions_api
from src.api import instances as instances_api
from src.api import pause as pause_api
from src.api import rules as rules_api
from src.api.freshness import ERROR, ensure_fresh
from src.curator import pause as pause_ops
from src.curator import runner
from src.db import state as state_read
from src.db.actions import insert_action, normalize_url, set_action_status
from src.db.settings_store import get_setting
from src.ext import protocol
from src.ext.commands import CommandError, send_command
from src.rules import access as rules_access


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- result size cap (shared by execute_js and get_text) ---------------------
# A page's innerText or a `document.querySelectorAll(...)` dump is routinely megabytes.
# Before this, the agent's only defence was writing `.slice(0, 1500)` into every snippet
# by hand — which it forgets exactly once, and then a single tool call floods its context.
# The server does the cutting instead, so the cap is a property of the TOOL rather than of
# whatever the agent remembered to type.
DEFAULT_MAX_BYTES = 40000


def _cut_utf8(raw: bytes, limit: int) -> str:
    """Decode ``raw[:limit]``, dropping an incomplete trailing UTF-8 sequence.

    ``errors="ignore"`` would silently drop bad bytes ANYWHERE; here the bad bytes possible
    are the 1-3 that a byte-boundary cut severed plus any surrogate escape
    :func:`_measure_utf8` had to encode, so ignoring them is exactly the intent — the
    alternative is ending every truncated payload in U+FFFD.
    """
    return raw[:limit].decode("utf-8", errors="ignore")


def _measure_utf8(text: str) -> bytes:
    """UTF-8 bytes of ``text``, tolerating a LONE SURROGATE.

    A page can genuinely hand one back — ``execute_js`` with ``"'\\ud800'"``, or such a
    char sitting in some element's ``textContent`` — and a plain ``.encode("utf-8")``
    raises ``UnicodeEncodeError`` on it. That escapes :func:`_guarded` (which catches only
    ToolError/HTTPException) and dies in the transport, so ONE malformed character on a
    page would take down a tool call that used to work: before the cap existed the value
    passed straight through and the MCP layer encoded it with ``ensure_ascii=True``, where
    a surrogate is harmless.

    ``surrogatepass`` keeps the measurement honest (the char is counted, at its 3-byte
    WTF-8 width) and never raises; the decode on the way out drops what it cannot represent.
    """
    return text.encode("utf-8", errors="surrogatepass")


def _validate_max_bytes(max_bytes: int | None) -> int:
    """Resolve ``max_bytes`` to a positive limit, refusing a bad one.

    Hoisted out of :func:`truncate_payload` so the capped verbs can call it BEFORE issuing
    their command: the cap is applied to the RESPONSE, so validating it there means an
    invalid argument costs a full round trip — during which the extension, reading
    ``maxBytes <= 0`` as "no limit", ships the entire innerText over the socket — and only
    then hears ``invalid_args``. Refusing an argument never required the browser.
    """
    limit = DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)
    if limit <= 0:
        raise ToolError("invalid_args", "max_bytes must be a positive integer")
    return limit


def truncate_payload(value, max_bytes: int | None = None) -> tuple:
    """Cap ``value`` at ``max_bytes`` of its UTF-8 size; return ``(value, meta)``.

    ``meta`` is ``{}`` when nothing was cut, else ``{"truncated": True, "total_bytes": N}``
    where ``N`` is the FULL size — the number the agent needs to decide whether to narrow
    its selector or page through the rest.

    Two shapes, because the two callers carry two shapes:

    * a ``str`` (``get_text``'s text, or a string ``execute_js`` result) is cut IN THE
      STRING, so what comes back is still readable text;
    * anything else is measured as JSON and, when it trips, handed back as
      ``{"__truncated_json": "<prefix>"}``. A cut JSON document is not valid JSON, so
      returning it as a STRING under an explicit marker is the honest option — the
      alternative is a structure that looks parseable and is not.

    Applied to the VALUE, never to the whole response envelope: cutting the envelope would
    take ``value`` / ``frames`` with it and leave the agent unable to address what it got.
    """
    limit = _validate_max_bytes(max_bytes)
    if isinstance(value, str):
        raw = _measure_utf8(value)
        if len(raw) <= limit:
            return value, {}
        return _cut_utf8(raw, limit), {"truncated": True, "total_bytes": len(raw)}
    # `default=str` so an exotic value the extension somehow sent (it should be
    # JSON-serializable already, chrome structured-clones it) can still be measured
    # instead of raising inside a size check.
    raw = _measure_utf8(json.dumps(value, ensure_ascii=False, default=str))
    if len(raw) <= limit:
        return value, {}
    return (
        {"__truncated_json": _cut_utf8(raw, limit)},
        {"truncated": True, "total_bytes": len(raw)},
    )


# --- caller-named command budgets (§6 + EXECUTE_JS_MAX_TIMEOUT_MS) -----------
def _clamp_timeout_ms(app, timeout_ms: int | None) -> int | None:
    """Clamp a caller's ``timeout_ms`` to ``EXECUTE_JS_MAX_TIMEOUT_MS``; ``None`` stays None.

    ``None`` means "the caller named nothing" and MUST keep meaning ``CMD_TIMEOUT_MS``
    downstream — that is what makes every new timeout parameter backwards compatible.
    """
    if timeout_ms is None:
        return None
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
        raise ToolError("invalid_args", "timeout_ms must be a positive integer")
    return min(timeout_ms, app.state.settings.execute_js_max_timeout_ms)


# How much longer the SOCKET budget is than the page-condition deadline of a waiting verb.
# THIS ORDERING IS THE WHOLE POINT OF wait_for: the extension polls until its own deadline
# and only then answers `timeout`, so if the service gave up at the same instant the
# command would die on the wire before the page condition could ever resolve — every wait
# would report `timeout` (the service's) instead of the truth. The margin covers the last
# poll interval plus the round trip.
_WAIT_SLACK_MS = 3000


def _wait_budget_ms(app, wait_ms: int) -> int:
    """The socket budget for a command that polls for ``wait_ms`` inside the extension."""
    return max(app.state.settings.cmd_timeout_ms, wait_ms + _WAIT_SLACK_MS)


class ToolError(Exception):
    """A tool refused or failed in a way the agent must see (returned, not raised
    out of the transport). ``code`` is a short machine string; ``payload`` carries
    any structured context (a preview, a pause deadline)."""

    def __init__(self, code: str, message: str, payload: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.payload = payload or {}
        super().__init__(f"{code}: {message}")


# --- stop gate (§12) ---------------------------------------------------------
async def _ensure_not_paused(app) -> None:
    """Raise :class:`ToolError` while the emergency stop is armed (§12).

    THE LINE THE GATE ACTUALLY DRAWS — and the message must say the same thing, because a
    refusal that misdescribes itself sends the reader looking for a bug: everything that
    REACHES THE BROWSER or writes persistent state is refused. That is every command verb
    (including the observing ones, ``get_text`` / ``wait_for``: they inject into pages and
    park the MV3 worker in a poll loop for up to a minute, which is exactly the "stop
    touching my browser" the stop means) plus the rule and exemption writers. What is NOT
    gated is a read that never leaves the DB — ``list_exemptions`` and friends — because
    looking at the state is precisely why one presses stop.
    """
    since = await app.state.db.read(pause_ops.read_stopped_at)
    if since is not None:
        raise ToolError(
            "stopped",
            "the curator is stopped; verbs that touch the browser or write state are "
            "refused until resume",
            {"stopped_at": since},
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
    # Mirror rows read after the waits so a just-landed snapshot is reflected.
    #
    # The WHOLE row is kept, not just ``snapshot_at``: this envelope REPLACED the
    # former list-of-mirror-rows, so anything dropped here loses its only MCP
    # surface. ``last_seen_at`` / ``reject_reason`` / ``reject_at`` answer "when did
    # this instance last speak" and "why was it cut off", and nothing else on the MCP
    # side answers them. ``focused_window_id`` is the one field deliberately NOT
    # carried over — ``list_windows`` (#46) reports the focused window per window,
    # which is strictly more useful than a bare id.
    rows = {i["id"]: i for i in await db.read(state_read._read_instances)}
    # session_id per active instance (#47): the epoch stamped alongside freshness so the
    # agent can pin it as expected_session on a later mutating verb.
    sessions = await db.read(state_read._read_active_sessions)
    # The §11 capability report: what each copy ALLOWS, as it declared in its last hello.
    # Carried here so an agent can read it BEFORE calling — the alternative is finding out
    # from a `js_disabled` halfway through a task.
    caps = await db.read(state_read._read_capabilities)
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
        row = rows.get(iid) or {}
        cap = caps.get(iid) or {}
        envelope[iid] = {
            "snapshot_at": row.get("snapshot_at"),
            # Capability report (§11/§12). ``allow_execute_js`` gates execute_js at the
            # extension edge; ``allow_debugger`` gates nothing yet (it is the switch a
            # later screenshot/CDP path reads) and is reported now so an agent never has
            # to learn a copy's answer by failing; ``ext_version`` is which bundle is
            # running — null when that copy has not said hello since the column landed.
            "allow_execute_js": cap.get("allow_execute_js"),
            "allow_debugger": cap.get("allow_debugger"),
            "ext_version": cap.get("ext_version"),
            "fresh": fresh,
            "reason": reason,
            "session_id": sessions.get(iid),
            # Carried over from the mirror row this envelope replaced (see above).
            "connected": row.get("connected"),
            "last_seen_at": row.get("last_seen_at"),
            "reject_reason": row.get("reject_reason"),
            "reject_at": row.get("reject_at"),
        }
    return envelope


async def list_instances(app) -> dict:
    """Per-instance freshness + ``stopped_at`` + ``resume_pending`` + ``pending_plan`` (§11).

    Awaits a fresh snapshot from every active instance (§6 blocking-fresh, via
    :func:`_freshen_fleet`) before answering, so the ``instances`` map carries a mirror
    the agent has just refreshed — each entry ``{snapshot_at, fresh, reason, session_id}``
    plus the mirror fields the envelope replaced. ``session_id`` is the #47 epoch to
    echo back as ``expected_session``.

    ``stopped_at`` lets the agent tell a deliberate stop from a broken curator (§11).
    The stop is INDEFINITE — there is no deadline and no countdown; the field is the
    moment it was pressed, ``null`` while running.

    ``resume_pending`` follows §7's visibility rule for the threshold latch: an armed
    over-threshold plan «выводится в статус-полосу и ждёт одного подтверждающего
    клика» — the human sees it on the startpage status bar, and this field is the
    agent's window into the same fact. While the latch is armed the curator
    deliberately defers its countable work; without the flag an agent sees no
    relocations happening and concludes the curator is broken. ``pending_plan`` is the
    SAME plan shape the startpage already sees in ``StateResponse`` (parsed by
    :func:`src.db.state._parse_pending_plan`) — the burst the human confirms the click
    BY — surfaced to the agent that only had the bare boolean before."""
    db = app.state.db
    instances = await _freshen_fleet(app)
    stopped_at = await db.read(pause_ops.read_stopped_at)
    resume_raw = await db.read(lambda c: get_setting(c, pause_ops.RESUME_PENDING_KEY))
    return {
        "server_now": _now_ms(),
        "stopped_at": stopped_at,
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
async def _command(app, instance, command, params, *, auth_ctx, expected_session=None,
                   cmd_timeout_ms=None):
    """Issue one extension command as an MCP verb. Every command carries
    ``initiator='mcp'`` and ``auth_ctx`` = the MCP session (§12: js_audit records the
    session, never a token id). Command failures are surfaced as a ``ToolError`` so
    the agent sees the §6 error code instead of a transport-level fault — including
    ``stale_session`` (#47), which rides the SAME generic mapping: when the agent pinned
    ``expected_session`` and the browser has since restarted, ``send_command`` stamps the
    old session, the extension refuses with ``stale_session``, and it reaches the agent as
    a ToolError like any other §6 code.

    ``cmd_timeout_ms`` overrides the global ``CMD_TIMEOUT_MS`` for THIS command only.
    ``None`` (every caller that does not pass it) keeps the global budget byte for byte;
    the waiting verbs pass a longer one, already clamped to ``EXECUTE_JS_MAX_TIMEOUT_MS``."""
    settings = app.state.settings
    try:
        return await send_command(
            app.state.ext_registry, app.state.db, instance, command, params,
            cmd_timeout_ms=(settings.cmd_timeout_ms if cmd_timeout_ms is None else cmd_timeout_ms),
            initiator="mcp", auth_ctx=auth_ctx,
            expected_session=expected_session,
        )
    except CommandError as exc:
        raise ToolError(exc.code, exc.message)


# --- #49 bulk plumbing -------------------------------------------------------
def _require_exactly_one_target(tab_id, tab_ids) -> None:
    """The tab_id / tab_ids XOR gate shared by close_tab / move_tab / relocate_tab (#49).

    EXACTLY ONE of the two must be given: both or neither -> ``invalid_args``. An empty
    ``tab_ids`` is ``invalid_args`` (a list form with nothing to do), and a DUPLICATE id
    inside ``tab_ids`` is ``invalid_args`` too — a duplicate would make the extension's
    per-item loop act on one tab twice (and duplicate the per-item ``actions`` row for
    relocate), so it is refused BEFORE any frame leaves."""
    if (tab_id is None) == (tab_ids is None):
        raise ToolError(
            "invalid_args", "exactly one of tab_id / tab_ids is required (not both, not neither)"
        )
    if tab_ids is not None:
        if len(tab_ids) == 0:
            raise ToolError("invalid_args", "tab_ids must be non-empty")
        if len(set(tab_ids)) != len(tab_ids):
            raise ToolError("invalid_args", "tab_ids contains duplicate ids")


def _reconcile_bulk_results(raw_results, items: list) -> list:
    """Match the extension's per-item ``results`` back to the input by ``index`` (#49).

    The extension answers ONE frame carrying ``{results:[{index, ok, ...}]}``; the match
    key is the ``index`` (position in the input list) because an ``open_tab`` item has no
    id before it opens. A SHORT/missing result — an item the extension never reported —
    is filled with ``{ok:false, error:"no_result"}`` (annotated with the requested
    ``tabId`` when the input item carried one, for cross-checking)."""
    by_index: dict = {}
    for r in raw_results or []:
        if isinstance(r, dict) and isinstance(r.get("index"), int):
            by_index.setdefault(r["index"], r)
    out: list = []
    for i, item in enumerate(items):
        r = by_index.get(i)
        if r is None:
            fill = {"index": i, "ok": False, "error": "no_result"}
            if isinstance(item, dict) and "tabId" in item:
                fill["tabId"] = item["tabId"]
            out.append(fill)
        else:
            out.append(r)
    return out


async def open_tab(app, *, instance: str, url: str, pinned: bool = False,
                   active: bool = False, window_id: int | None = None,
                   lease_ttl_s: int | None = None,
                   auth_ctx: str | None = None,
                   expected_session: str | None = None) -> dict:
    """Open a tab in an instance (§6), optionally in a NAMED window (#45).

    ``window_id`` is optional: absent, this is exactly today's behaviour — the extension
    auto-selects the §9 window (the curator's own pass never names one). Given, the
    extension is asked to open in THAT window and validates it with §9's mergeable
    predicate (``no_window`` for a popup/devtools/app/fullscreen or a vanished window).

    The SERVER-SIDE cross-check is required, not belt-and-suspenders: an OLD extension
    silently IGNORES the unknown ``windowId`` frame key, drops the tab in its OWN window
    and still answers ``ok``. The service and the extension update by different paths
    (the Dockerfile does not ship ``extension/``), so "new service + old extension" is a
    guaranteed state — so we compare the ``windowId`` the extension actually reports to
    the one we asked for and turn a mismatch into a loud ``no_window``. The extension
    cannot do this itself: to it the request never said "here", it was an unknown key.

    ``lease_ttl_s`` writes an ``exemptions`` row for this url on a successful open — the
    "owned by the agent" lease. Without it, a tab the agent opens for a task is fair game
    for the very next pass, which may relocate or collapse it mid-task.

    THE SPLIT INSIDE THAT LEASE, which is not the same for both halves:

    * its ARGUMENTS (``url``, ``ttl_s``) are validated FIRST, before the tab is opened, and
      a bad one is a hard ``invalid_args`` with no frame sent. Judging an argument never
      required a tab to exist, and ``lease_ttl_s=0`` answering ``ok: true`` with
      ``lease: {ok: false}`` — while ``set_exemption`` refuses the very same value — would
      make the contract depend on which door the agent knocked at;
    * the WRITE afterwards is best-effort and reported as ``lease: {ok:false, ...}`` without
      failing the call. Here soft degradation is the honest answer: the tab IS open, and
      saying otherwise would invite the agent to open a second one.
    """
    await _ensure_not_paused(app)
    # Validated up front, its result reused below: re-deriving it after the open would run
    # the ceiling and the url rule twice and let the two answers drift.
    lease_plan = _plan_open_lease(url, lease_ttl_s) if lease_ttl_s is not None else None
    params: dict = {"url": url, "pinned": bool(pinned), "active": bool(active)}
    if window_id is not None:
        params["windowId"] = window_id
    result = await _command(
        app, instance, protocol.CMD_OPEN_TAB, params, auth_ctx=auth_ctx,
        expected_session=expected_session,
    )
    if window_id is not None:
        actual = result.get("windowId")
        if actual != window_id:
            raise ToolError(
                protocol.ERR_NO_WINDOW,
                f"open_tab landed in window {actual!r}, not the requested {window_id!r} "
                "(the window vanished, or this extension predates window addressing)",
            )
    out = {"ok": True, "result": result}
    if lease_plan is not None:
        out["lease"] = await _write_open_lease(app, instance, *lease_plan)
    return out


async def close_tab(app, *, instance: str, tab_id: int | None = None,
                    tab_ids: list[int] | None = None,
                    auth_ctx: str | None = None,
                    expected_session: str | None = None) -> dict:
    """Close ONE tab (``tab_id``, unchanged response shape) or a LIST (``tab_ids``, #49).

    Bulk sends ONE ``close_tab {items}`` frame; the extension loops it and answers a
    per-item ``results`` array. The bulk close sends the SAME ``expect`` as the single
    form — i.e. today NONE: hardening only the bulk path would be a hole (a bulk call
    that refused an audible tab, retried single, would close it — the guard bypassed by
    one retry). Tightening the single-close guards is a separate decision, not made here.

    ``timeout`` / ``no_connection`` on the LIST form is UNKNOWN — the extension answers one
    frame at the very end, so a connection-class failure says nothing about which items
    were closed. Do NOT blindly retry the whole list; the truth comes from the next
    ``list_tabs`` (blocking-fresh)."""
    await _ensure_not_paused(app)
    _require_exactly_one_target(tab_id, tab_ids)
    if tab_ids is None:
        result = await _command(
            app, instance, protocol.CMD_CLOSE_TAB, {"tabId": tab_id}, auth_ctx=auth_ctx,
            expected_session=expected_session,
        )
        return {"ok": True, "result": result}
    items = [{"tabId": t} for t in tab_ids]  # no `expect` — behaves like single close
    result = await _command(
        app, instance, protocol.CMD_CLOSE_TAB, {"items": items}, auth_ctx=auth_ctx,
        expected_session=expected_session,
    )
    return {"ok": True, "results": _reconcile_bulk_results(result.get("results"), items)}


async def focus_tab(app, *, instance: str, tab_id: int,
                    auth_ctx: str | None = None,
                    expected_session: str | None = None) -> dict:
    await _ensure_not_paused(app)
    result = await _command(
        app, instance, protocol.CMD_FOCUS_TAB, {"tabId": tab_id}, auth_ctx=auth_ctx,
        expected_session=expected_session,
    )
    return {"ok": True, "result": result}


async def move_tab(app, *, instance: str, tab_id: int | None = None,
                   tab_ids: list[int] | None = None, window_id: int | None = None,
                   index: int | None = None, auth_ctx: str | None = None,
                   expected_session: str | None = None) -> dict:
    """Move one tab to a window/position INSIDE one browser (§6/§9), or — with
    ``window_id=None`` — EXTRACT it into a brand-new background window (#45).

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

    ``window_id=None`` addresses "extract into a NEW window" (#45): it rides the frame
    verbatim as ``windowId: null`` and the extension calls ``windows.create({tabId})``,
    returning the created window's id in the response ``windowId``. The pinned guard
    applies there too (that create strips ``pinned`` down the same Chromium path), and an
    OLD extension refuses ``null`` loudly at its ``Number.isInteger(windowId)`` guard with
    ``precondition_failed`` (it never learned that null means "new window"), so the
    migration is safe — a loud refusal, never a silent misplacement.

    No ``actions`` row: this follows its siblings ``open_tab`` / ``close_tab`` /
    ``focus_tab``, which journal nothing from the MCP door either. (``merge_windows``
    does, because §9 requires the manual merge to be recorded and it shares that
    writer with the HTTP button.)
    """
    await _ensure_not_paused(app)
    _require_exactly_one_target(tab_id, tab_ids)
    if tab_ids is None:
        params: dict = {"tabId": tab_id, "windowId": window_id}
        if index is not None:
            params["index"] = index
        result = await _command(app, instance, protocol.CMD_MOVE_TAB, params, auth_ctx=auth_ctx,
                                expected_session=expected_session)
        return {"ok": True, "result": result}
    # #49 bulk: ONE shared target window for the whole list. ``window_id=None`` (extract
    # into a NEW window) is refused for a list — ``windows.create`` takes ONE tabId, so
    # "one new window for all" vs "N windows" is a different, unrequested op.
    if window_id is None:
        raise ToolError(
            "invalid_args",
            "tab_ids with window_id:null (extract-to-new) is not supported — that is a "
            "different, unrequested op",
        )
    items = [{"tabId": t} for t in tab_ids]
    params = {"items": items, "windowId": window_id}
    if index is not None:
        params["index"] = index
    result = await _command(app, instance, protocol.CMD_MOVE_TAB, params, auth_ctx=auth_ctx,
                            expected_session=expected_session)
    return {"ok": True, "results": _reconcile_bulk_results(result.get("results"), items)}


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


def _flatten_injection_results(raw_results, max_bytes: int | None) -> dict:
    """Turn chrome's ``[InjectionResult]`` into ``{value, frames}`` (+ truncation meta).

    ``chrome.scripting.executeScript`` answers one ``{frameId, documentId, result}`` per
    frame, and the old verb handed that array straight through inside ``{"result": {...}}``.
    Through MCP that arrived as JSON inside JSON inside JSON — three layers of escaping for
    what is, in the overwhelming majority of calls, ONE value from ONE frame.

    So: ``value`` is the MAIN frame's result (``frameId == 0``, or the first entry when no
    frame 0 is reported — a same-shape answer beats an empty one).

    ``frames`` is present ONLY when there is genuinely more than one, and each entry is
    capped separately so a giant result in one sub-frame cannot swallow the main frame's
    answer. Today the extension targets ``{tabId}`` and never sets ``allFrames``, so one
    entry is what every call produces — emitting it anyway would repeat ``value`` verbatim
    and, since the cap is per entry, let a truncated response weigh 2x ``max_bytes``:
    a byte cap that doubles the payload it was added to bound. The key stays in the shape
    for the day an all-frames injection exists; until then its absence means "one frame".
    """
    entries = [r for r in (raw_results or []) if isinstance(r, dict)]
    main = next((r for r in entries if r.get("frameId") == 0), None)
    if main is None and entries:
        main = entries[0]

    value, meta = truncate_payload(main.get("result") if main else None, max_bytes)
    out = {"value": value, **meta}
    if len(entries) > 1:
        # Reuse the already-cut main value rather than truncating it a second time.
        out["frames"] = [
            {"frame_id": main.get("frameId"), "value": value, **meta}
            if r is main
            else _one_frame(r, max_bytes)
            for r in entries
        ]
    return out


def _one_frame(entry: dict, max_bytes: int | None) -> dict:
    fvalue, fmeta = truncate_payload(entry.get("result"), max_bytes)
    return {"frame_id": entry.get("frameId"), "value": fvalue, **fmeta}


async def execute_js(app, *, instance: str, tab_id: int, code: str,
                     world: str | None = None, url_at_exec: str | None = None,
                     await_promise: bool = False, timeout_ms: int | None = None,
                     max_bytes: int | None = None,
                     auth_ctx: str | None = None,
                     expected_session: str | None = None) -> dict:
    """Run JS in a tab as an MCP verb. ``send_command`` writes the js_audit row
    BEFORE the send and enforces the runtime kill-switch (§12): a disabled/rejected
    call is still audited (with ``initiator='mcp'`` + the MCP ``auth_ctx``), and the
    extension's own execute_js checkbox still gates it at the edge. Refused while
    paused.

    ``await_promise`` (default False) makes the code the body of an async function in the
    page, so top-level ``await`` and ``return`` both work and the promise is awaited before
    the value comes back. Without it the code goes through indirect eval exactly as before
    — which supports neither, and is why async snippets used to answer ``null``.

    ``timeout_ms`` raises THIS command's socket budget (clamped to
    ``EXECUTE_JS_MAX_TIMEOUT_MS``) because async page code legitimately outlives
    ``CMD_TIMEOUT_MS``; omitted, the global budget applies unchanged.

    The result is FLAT: ``value`` (plus ``frames`` only when there is more than one)
    instead of the raw ``{results:[...]}``, capped at ``max_bytes`` (default 40 kB) — see
    :func:`_flatten_injection_results`."""
    await _ensure_not_paused(app)
    budget = _clamp_timeout_ms(app, timeout_ms)
    # VALIDATE BEFORE THE ROUND TRIP. `max_bytes` is only enforced after the response comes
    # back, so a bad value used to cost a full command — and for THIS verb also a durable
    # js_audit row recording code that was never going to be delivered.
    _validate_max_bytes(max_bytes)
    params: dict = {"tabId": tab_id, "code": code}
    if world is not None:
        params["world"] = world
    if url_at_exec is not None:
        params["urlAtExec"] = url_at_exec
    # Sent ONLY when true, so an unchanged call puts an unchanged frame on the wire and an
    # older extension sees exactly the params it always saw.
    if await_promise:
        params["awaitPromise"] = True
    result = await _command(app, instance, protocol.CMD_EXECUTE_JS, params, auth_ctx=auth_ctx,
                            expected_session=expected_session, cmd_timeout_ms=budget)
    return {"ok": True, **_flatten_injection_results(result.get("results"), max_bytes)}


async def get_text(app, *, instance: str, tab_id: int, selector: str | None = None,
                   max_bytes: int | None = None, auth_ctx: str | None = None,
                   expected_session: str | None = None) -> dict:
    """Read a tab's text — the cheap read that used to require ``execute_js`` (§11).

    NOT behind the execute_js checkbox and writing NO ``js_audit`` row, and that is a
    deliberate line rather than an oversight: the gate exists because ARBITRARY code
    arrives at ``execute_js`` and §12's argument is that truncated code cannot be
    reconstructed after the fact. This verb injects a FIXED function that is committed into
    the extension and known at build time — there is nothing to reconstruct. Every other
    gate still applies: the stop gate here, the revoke check and ``stale_session`` in
    ``send_command``, and the extension's http/https edge guard on the target tab.

    A ``selector`` that matches nothing is ``precondition_failed``, never an empty string:
    "your selector is wrong" and "the page is blank" are different facts, and conflating
    them sends the agent to debug the wrong one.

    ``max_bytes`` (default 40 kB) rides down to the extension — which cuts at the source,
    so a 10 MB innerText never crosses the socket and ``total_bytes`` is the TRUE size —
    and is then re-applied here by the shared cap, which is what enforces it for an older
    extension that ignores the parameter."""
    await _ensure_not_paused(app)
    # Before the round trip: an extension reading `maxBytes <= 0` as "no limit" would ship
    # the whole innerText across the socket, and only then would the cap refuse it here.
    limit = _validate_max_bytes(max_bytes)
    params: dict = {"tabId": tab_id, "maxBytes": limit}
    if selector is not None:
        params["selector"] = selector
    result = await _command(app, instance, protocol.CMD_GET_TEXT, params, auth_ctx=auth_ctx,
                            expected_session=expected_session)
    text, meta = truncate_payload(result.get("text") or "", limit)
    # The EXTENSION's number wins when it already cut: it measured the whole document, so
    # its totalBytes is the real size, where a server-side re-measure could only report the
    # size of what already arrived. `totalBytes` is camelCase on the WIRE like every other
    # §6 key; the MCP-facing name is snake_case.
    #
    # Falling back to the local measurement when the number is missing: "truncated: true,
    # total_bytes: null" is the tool saying "I cut it and I won't say by how much". An
    # under-count from an extension that reported the flag without the size is still a
    # floor the agent can act on.
    if result.get("truncated"):
        reported = result.get("totalBytes")
        if not isinstance(reported, int) or isinstance(reported, bool):
            reported = meta.get("total_bytes", len(_measure_utf8(text)))
        meta = {"truncated": True, "total_bytes": reported}
    return {"ok": True, "text": text, **meta}


async def wait_for(app, *, instance: str, tab_id: int, url_matches: str | None = None,
                   selector: str | None = None, text_contains: str | None = None,
                   timeout_ms: int | None = None, auth_ctx: str | None = None,
                   expected_session: str | None = None) -> dict:
    """Wait until a page condition holds; answer ``{ok, matched, elapsed_ms}`` (§11).

    A DEADLINE THAT PASSES IS ``matched: False``, NOT an error. §11 fixes ``timeout`` to
    mean UNKNOWN — no frame arrived, the browser may be wedged, do not blindly retry — and
    a wait that ran its full course is the opposite: the browser answered, and "no, it
    never became true" is a definite negative the agent can act on. Two different facts get
    two different response SHAPES, because a shape is what survives the wire; a shared error
    string would ask the agent to tell them apart by reading prose. A transport ``timeout``
    can still happen here and still means unknown.

    EXACTLY ONE of ``url_matches`` / ``selector`` / ``text_contains``. Zero or several is
    ``invalid_args`` and NOTHING is sent: "wait for A and B" and "wait for A or B" are
    different verbs, and guessing which was meant would spend the whole budget answering a
    question nobody asked.

    Like :func:`get_text` this injects a FIXED function (for ``selector`` /
    ``text_contains``; ``url_matches`` needs no injection at all), so it is NOT behind the
    execute_js checkbox and writes no ``js_audit`` row — see that docstring for why.

    THE ORDERING THAT MAKES THE VERB WORK: the extension polls until ITS deadline and only
    then reports the negative verdict, so the SOCKET budget must outlast that deadline
    (:func:`_wait_budget_ms`). Give both the same number and every wait dies on the wire
    first, reporting the service's ``timeout`` — "unknown" — instead of the page's actual
    answer, which is strictly worse than not having the verb."""
    await _ensure_not_paused(app)
    given = [
        name for name, value in (
            ("url_matches", url_matches),
            ("selector", selector),
            ("text_contains", text_contains),
        ) if value is not None
    ]
    if len(given) != 1:
        raise ToolError(
            "invalid_args",
            "exactly one of url_matches / selector / text_contains is required "
            f"(got {len(given)}: {given or 'none'})",
        )
    # An omitted timeout means "the ceiling", not "CMD_TIMEOUT_MS": a wait with no stated
    # length wants the longest one the operator permits, where every other verb wants the
    # ordinary command budget.
    wait_ms = _clamp_timeout_ms(app, timeout_ms)
    if wait_ms is None:
        wait_ms = app.state.settings.execute_js_max_timeout_ms

    params: dict = {"tabId": tab_id, "timeoutMs": wait_ms}
    if url_matches is not None:
        params["urlMatches"] = url_matches
    if selector is not None:
        params["selector"] = selector
    if text_contains is not None:
        params["textContains"] = text_contains
    result = await _command(
        app, instance, protocol.CMD_WAIT_FOR, params, auth_ctx=auth_ctx,
        expected_session=expected_session, cmd_timeout_ms=_wait_budget_ms(app, wait_ms),
    )
    # Renamed, not splatted: `elapsedMs` is the WIRE spelling (§6 is camelCase throughout),
    # and everything the agent reads is snake_case.
    return {
        "ok": True,
        "matched": bool(result.get("matched")),
        "elapsed_ms": int(result.get("elapsedMs") or 0),
    }


async def navigate_tab(app, *, instance: str, tab_id: int, url: str,
                       wait_until: str | None = None, selector: str | None = None,
                       timeout_ms: int | None = None, auth_ctx: str | None = None,
                       expected_session: str | None = None) -> dict:
    """Point a tab at a url (§6), optionally waiting for the page to be there.

    ``wait_until`` defaults to ``'none'`` — the frame then carries no wait key at all and
    the extension behaves exactly as it always has (issue the update, answer ``{ok:true}``).
    ``'load'`` waits for the tab to report ``complete``; ``'selector'`` waits for
    ``selector`` to match. A wait that expires is a SUCCESS whose ``result`` carries
    ``matched: false`` — never an error: the tab was pointed at the url either way, so the
    negative is a verdict, not the "state unknown" that ``timeout`` reserves (see
    :func:`wait_for`).

    The http/https edge guard is the extension's and is unchanged (§12: a caller must not
    be able to steer a tab to ``data:``/``javascript:``). §7/§5 note, also unchanged: the
    navigation's document change stamps the tab's activity clock, so a navigated tab reads
    as freshly touched — it errs SAFE (a too-fresh tab is never wrongly closed) and
    self-heals within IDLE_MINUTES."""
    await _ensure_not_paused(app)
    params: dict = {"tabId": tab_id, "url": url}
    budget = None
    if wait_until is not None and wait_until != "none":
        wait_ms = _clamp_timeout_ms(app, timeout_ms)
        if wait_ms is None:
            wait_ms = app.state.settings.execute_js_max_timeout_ms
        params["waitUntil"] = wait_until
        params["timeoutMs"] = wait_ms
        if selector is not None:
            params["selector"] = selector
        budget = _wait_budget_ms(app, wait_ms)  # same ordering rule as wait_for
    result = await _command(app, instance, protocol.CMD_NAVIGATE_TAB, params, auth_ctx=auth_ctx,
                            expected_session=expected_session, cmd_timeout_ms=budget)
    return {"ok": True, "result": result}


# --- relocate: synchronous open + guarded source close in one call (#48, §11) ---
def _read_relocate_inputs(instance_from: str, tab_id: int, instance_to: str):
    """Reader ``fn(conn)``: the source tab row (incl. pinned/audible/active), both
    instances' current sessions, and the SOURCE instance's focused window id.

    Returns ``(tab, session_from, session_to, focused_window_id_from)`` where ``tab`` is
    the source tabs row (or None). The sessions come from the ``instances`` mirror; the
    caller prefers a live ``ConnState`` session when one exists (same order phase A uses).
    ``pinned`` / ``audible`` / ``active`` and ``focused_window_id_from`` are read (#48) so
    the caller can refuse a source that phase B's step-4 close guards could never close —
    BEFORE it opens an un-closeable copy."""
    import sqlite3

    def _fn(conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        tab = conn.execute(
            "SELECT instance_id, tab_id, window_id, url, title, opened_at, "
            "last_active_at, age_unknown, pinned, audible, active "
            "FROM tabs WHERE instance_id = ? AND tab_id = ?",
            (instance_from, tab_id),
        ).fetchone()
        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, session_id, focused_window_id FROM instances "
                "WHERE id IN (?, ?)",
                (instance_from, instance_to),
            ).fetchall()
        }
        src = rows.get(instance_from)
        dst = rows.get(instance_to)
        return (
            tab,
            src["session_id"] if src is not None else None,
            dst["session_id"] if dst is not None else None,
            src["focused_window_id"] if src is not None else None,
        )

    return _fn


def _read_bulk_relocate_inputs(instance_from: str, tab_ids: list[int], instance_to: str):
    """Reader ``fn(conn)`` for the BULK relocate (#49): every source tab row keyed by
    ``tab_id``, both instances' sessions, the source's focused window id, AND the set of
    FULL url strings the TARGET already holds (for the batch dedup).

    Returns ``(tabs_by_id, session_from, session_to, focused_window_id_from, target_urls)``.
    One read for the whole list — the per-item work in the loop touches no DB."""
    import sqlite3

    def _fn(conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in tab_ids)
        tab_rows = conn.execute(
            "SELECT instance_id, tab_id, window_id, url, title, opened_at, "
            "last_active_at, age_unknown, pinned, audible, active "
            f"FROM tabs WHERE instance_id = ? AND tab_id IN ({placeholders})",
            (instance_from, *tab_ids),
        ).fetchall()
        tabs_by_id = {r["tab_id"]: r for r in tab_rows}
        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, session_id, focused_window_id FROM instances WHERE id IN (?, ?)",
                (instance_from, instance_to),
            ).fetchall()
        }
        src = rows.get(instance_from)
        dst = rows.get(instance_to)
        target_urls = {
            r["url"]
            for r in conn.execute(
                "SELECT url FROM tabs WHERE instance_id = ?", (instance_to,)
            ).fetchall()
        }
        return (
            tabs_by_id,
            src["session_id"] if src is not None else None,
            dst["session_id"] if dst is not None else None,
            src["focused_window_id"] if src is not None else None,
            target_urls,
        )

    return _fn


async def relocate_tab(app, *, instance_from: str, tab_id: int | None = None,
                       tab_ids: list[int] | None = None, instance_to: str,
                       auth_ctx: str | None = None,
                       expected_session_from: str | None = None) -> dict:
    """MCP-initiated relocation, completed SYNCHRONOUSLY in one call (#48).

    The MCP verb now attempts BOTH phases: phase A opens the copy in the target and
    writes the live ``relocate`` row, and — new here — a synchronous phase B closes the
    source under §7's step-4 volatile close guards. It walks this state machine:

    * ``opening``  — the guards pass and the copy is opened;
    * ``closing``  — the copy is open, the rows are written, the source close is sent;
    * ``done``     — the source closed: the pair ``relocate``(done) + ``relocate_close``
      (done) is journalled and the source mirror row is removed;
    * ``half``     — the copy is open but the source close could not be completed
      (precondition / uncertain / copy vanished): this is TODAY'S normal state, NOT an
      error — the source is left alive and the pass's phase B / reconcile finishes it
      later. The synchronous path degrades to exactly what phase-A-only used to do.

    The curator pass stays two-phase; nothing in ``src/curator/`` changes. Undo works
    through a synthetic ``pass_id`` (``mcp-<uuid>``) stamped on both halves and returned
    as ``undo_pass_id`` — no ``passes`` row is written, so metrics and ``_read_last_pass``
    never see it and a double undo is safe (``restored_at`` → ``already_undone``).

    ``tab_ids`` (#49) relocates a LIST as one unit: ONE ``open_tab {items}`` frame to the
    target then ONE ``close_tab {items}`` frame to the source, each item running the #48
    machinery independently and getting its own ``status`` (done/half). The whole call
    shares ONE ``pass_id`` so ``undo_pass_id`` reverses the batch as a unit. See
    :func:`_relocate_bulk`."""
    await _ensure_not_paused(app)  # guard 1: the stop switch — a paused curator refuses.
    _require_exactly_one_target(tab_id, tab_ids)
    if tab_ids is not None:
        return await _relocate_bulk(
            app, instance_from=instance_from, tab_ids=tab_ids, instance_to=instance_to,
            auth_ctx=auth_ctx, expected_session_from=expected_session_from,
        )
    db, registry = app.state.db, app.state.ext_registry

    tab, db_session_from, db_session_to, focused_window_id_from = await db.read(
        _read_relocate_inputs(instance_from, tab_id, instance_to)
    )
    if tab is None:  # guard 2: the tab must be in the mirror.
        raise ToolError("no_such_tab", f"no mirrored tab {tab_id} on {instance_from}")

    # Prefer the live ConnState session (freshest), fall back to the mirror — the
    # same precedence phase A uses. These sessions are recorded on the relocate row;
    # the pass only completes it while BOTH still match (mirror.py liveness rule).
    cs_from = registry.get(instance_from)
    cs_to = registry.get(instance_to)
    session_from = cs_from.session_id if cs_from is not None else db_session_from
    session_to = cs_to.session_id if cs_to is not None else db_session_to

    # guard 3, #47 session epoch — SOURCE side. A pre-check BEFORE phase A opens any copy:
    # a mismatch means the source browser has restarted since the agent read the session,
    # its ``tab_id`` is from a dead epoch, and relocating it would copy a stale tab and
    # later close the wrong one. Refuse with ``stale_session`` and touch nothing. The
    # recorded ``session_id_from`` still carries this epoch onto the relocate row, and the
    # synchronous source close below STAMPS ``expected_session_from`` (see step 6) so a
    # source restart AFTER this check is refused by the extension edge, not acted on.
    if expected_session_from is not None and expected_session_from != session_from:
        raise ToolError(
            protocol.ERR_STALE_SESSION,
            f"source session for {instance_from} is not {expected_session_from!r} "
            "(the source browser restarted); relocation refused",
        )

    # guard 4 (#48): a pinned / audible / active-in-focused-window source can NEVER be
    # closed by phase B's step-4 close guards (notPinned / notAudible / not-active-in-
    # focus). Opening the copy first and only THEN discovering the source is un-closeable
    # would leave the copy live and strike the source pair toward quarantine on every
    # refused close. Refuse BEFORE ``open_tab`` — nothing is opened, nothing written.
    if tab["pinned"]:
        raise ToolError(
            protocol.ERR_PRECONDITION_FAILED,
            f"source tab {tab_id} on {instance_from} is pinned; not relocatable",
        )
    if tab["audible"]:
        raise ToolError(
            protocol.ERR_PRECONDITION_FAILED,
            f"source tab {tab_id} on {instance_from} is audible; not relocatable",
        )
    if (
        tab["active"]
        and tab["window_id"] is not None
        and tab["window_id"] == focused_window_id_from
    ):
        raise ToolError(
            protocol.ERR_PRECONDITION_FAILED,
            f"source tab {tab_id} on {instance_from} is active in the focused window; "
            "not relocatable",
        )

    now = _now_ms()
    url = tab["url"]
    url_norm = normalize_url(url)
    seed_age_ms = now - tab["last_active_at"]
    seed_opened_ago_ms = now - tab["opened_at"]

    # step 2: phase A — open the copy in the target (async, outside any txn).
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

    # The synthetic pass id (#48) — ONE per verb call, generated BEFORE the insert (never
    # from lastrowid) and stamped on BOTH halves so ``undo_pass`` reaches the pair via
    # ``_read_pass_actions``.
    pass_id = f"mcp-{uuid4()}"

    # The seed clocks the copy inherits from the source (so it is not "younger" for
    # dedup/idle/singleton), mirroring phase A's copy-tab row.
    seed = SimpleNamespace(
        url=url, title=tab["title"], opened_at=tab["opened_at"],
        last_active_at=tab["last_active_at"], age_unknown=tab["age_unknown"],
    )

    # steps 3+4: ONE transaction — the copy's mirror row + the live ``relocate``(done)
    # row + the ``relocate_close``(pending) marker. These are written TOGETHER (not in two
    # commits) on purpose: ``load_mirror`` treats a ``relocate`` with no done/pending
    # relocate_close as a LIVE phase-B candidate (mirror.py), so a relocate committed
    # WITHOUT its pending marker — even for one await tick — would let a concurrent pass
    # (passes are not serialized against MCP verbs) capture that mirror and issue its own
    # source close, racing this verb's step-6 close. The pending marker is phase B's
    # at-least-once pattern: written BEFORE the browser close so a crash between a
    # successful close and the completion write never leaves the source closed with no
    # journal row. A concurrent pass's reconcile will not touch this pending row while the
    # verb may still be awaiting the close — ``read_pending_closes`` skips rows younger
    # than the open+get_tab+close round-trip budget.
    def _write_pair(conn):
        # Reuse phase A's copy-tab UPSERT so the copy's mirror row is identical.
        from src.curator.phases import _insert_copy_tab

        _insert_copy_tab(
            conn, instance_id=instance_to, tab_id=tab_id_to, window_id=window_id_to,
            tab=seed, now=now,
        )
        relocate_id = insert_action(
            conn, ts=now, kind="relocate", status="done", initiator="mcp",
            pass_id=pass_id,
            instance_from=instance_from, instance_to=instance_to,
            tab_id=tab["tab_id"], session_id_from=session_from,
            tab_id_to=tab_id_to, session_id_to=session_to,
            decision="mcp_relocate",
            src_opened_at=tab["opened_at"], src_last_active_at=tab["last_active_at"],
            src_age_unknown=tab["age_unknown"],
            url=url, url_norm=url_norm, title=tab["title"], pinned=0,
        )
        close_id = insert_action(
            conn, ts=now, kind="relocate_close", status="pending", initiator="mcp",
            pass_id=pass_id, origin_action_id=relocate_id,
            instance_from=instance_from, instance_to=instance_to,
            tab_id=tab["tab_id"], session_id_from=session_from,
            tab_id_to=tab_id_to, session_id_to=session_to,
            url=url, url_norm=url_norm, title=tab["title"],
        )
        return relocate_id, close_id

    action_id, pending_id = await db.write(_write_pair)
    base = {
        "ok": True, "action_id": action_id, "tab_id_to": tab_id_to,
        "instance_to": instance_to, "undo_pass_id": pass_id,
    }

    # step 5: COPY CHECK — the copy must still exist before we close the source. A target
    # restart in the window between the open and now would have destroyed the copy;
    # closing the source then loses the tab irrecoverably. ``get_tab`` to the TARGET (no
    # ``expected_session`` — reading a restarted target is fine). ``no_such_tab`` ⇒
    # degrade to ``half`` (``copy_gone``), source untouched. The leftover ``pending``
    # relocate_close is resolved by a later pass's reconcile (source present ⇒ abandoned;
    # the relocate then re-enters ``live_relocations`` and phase B abandons it once its own
    # ``get_tab`` also finds the copy gone) — self-healing, never a closed source.
    try:
        await _command(
            app, instance_to, protocol.CMD_GET_TAB, {"tabId": tab_id_to},
            auth_ctx=auth_ctx,
        )
    except ToolError as exc:
        reason = "copy_gone" if exc.code == protocol.ERR_NO_SUCH_TAB else exc.code
        return {**base, "status": "half", "reason": reason}

    # step 6: close the SOURCE with the step-4 volatile guards — url / notAudible /
    # notPinned — but deliberately NO ``minIdleMs``. The idle threshold protects AUTOMATION
    # from a tab a human is using; here the agent explicitly chose this tab, and minIdleMs
    # would refuse exactly the tab it asked to move (today's MCP close sends no ``expect``
    # at all, so this is a tightening). ``expected_session_from`` (#47) is stamped so a
    # source restart between the guard-3 pre-check and here is refused (``stale_session``)
    # by the extension edge instead of closing the wrong tab.
    expect = {"url": url, "notAudible": True, "notPinned": True}
    try:
        await _command(
            app, instance_from, protocol.CMD_CLOSE_TAB,
            {"tabId": tab["tab_id"], "expect": expect},
            auth_ctx=auth_ctx, expected_session=expected_session_from,
        )
    except ToolError as exc:
        if exc.code == protocol.ERR_PRECONDITION_FAILED:
            # The source turned pinned/audible/active — it did NOT close. Fail the pending
            # row; the relocation stays a live ``half`` and the pass's phase B retries it
            # (a FAILED relocate_close does not retire the relocate row, mirror.py).
            await db.write(
                lambda c: set_action_status(c, pending_id, "failed", reason=exc.code)
            )
            return {**base, "status": "half", "reason": exc.code}
        if exc.code == protocol.ERR_NO_SUCH_TAB:
            # The source is already gone (someone closed it): the goal is reached.
            # Complete the relocation — pending → done — and drop the stale source row so
            # it is not left live.
            def _done_gone(conn):
                set_action_status(
                    conn, pending_id, "done", reason=protocol.ERR_NO_SUCH_TAB
                )
                _delete_source_tab(conn, instance_from, tab["tab_id"])

            await db.write(_done_gone)
            return {**base, "status": "done"}
        # Connection-class (no_connection / timeout) or a stale-session refusal: the close
        # is UNCERTAIN or did not happen. Leave the ``pending`` row for the next pass's
        # reconcile and report ``half`` with the code.
        return {**base, "status": "half", "reason": exc.code}

    # The source closed => complete: pending → done, drop the source mirror row.
    def _done(conn):
        set_action_status(conn, pending_id, "done")
        _delete_source_tab(conn, instance_from, tab["tab_id"])

    await db.write(_done)
    return {**base, "status": "done"}


async def _relocate_bulk(app, *, instance_from: str, tab_ids: list[int], instance_to: str,
                         auth_ctx: str | None, expected_session_from: str | None) -> dict:
    """Relocate a LIST from ``instance_from`` to ``instance_to`` in THREE frames (#49).

    ONE ``open_tab {items}`` to the target, ONE ``get_tab {items}`` copy-check to the target,
    then ONE ``close_tab {items}`` to the source; each item runs #48's synchronous machinery
    independently. The whole call shares ONE ``pass_id = mcp-<uuid>``, so undo reverses the
    batch as a unit.

    Per-item outcome (``results[i]`` keyed by position in ``tab_ids``):

    * a source that phase B could NEVER close — pinned / audible / active-in-focus — is
      refused BEFORE its copy opens: ``ok:false`` (opening an un-closeable copy would
      strike the pair toward quarantine on every refused close, as in #48 guard 4);
    * DEDUP is required: ``decide`` protects a pass from opening two copies of one address,
      but this ``open_tab {items}`` frame has no such guard — five sources with one url
      would make five permanent copies. Items are deduped by FULL url string against BOTH
      what the target already holds AND earlier kept items in this batch; a dropped item
      gets ``ok:false, error:"duplicate"``;
    * an item whose phase A (open) failed gets ``ok:false``;
    * an opened copy whose source closed is ``ok:true, status:"done"``; one whose source
      close could not complete is ``ok:true, status:"half"`` — today's normal state, the
      pass's phase B finishes it later;
    * an opened copy that VANISHED before the source close (a target restart caught by the
      get_tab copy-check) is ``ok:true, status:"half", reason:"copy_gone"`` — its source is
      NOT closed and its relocate_close is left ``pending`` (reconcile abandons it once it
      finds the source present), never an irrecoverable loss.

    ``timeout`` / ``no_connection`` on either frame is UNKNOWN. On the OPEN frame nothing is
    written and the ToolError propagates; on the CLOSE frame the copies are already open
    and the ``pending`` rows written, so every live item is reported ``half`` (the pass's
    reconcile completes them) rather than raising — but the caller must still treat the
    connection-class ``reason`` as UNKNOWN and re-read ``list_tabs``, never blind-retry."""
    db, registry = app.state.db, app.state.ext_registry

    tabs_by_id, db_session_from, db_session_to, focused_window_id_from, target_urls = await db.read(
        _read_bulk_relocate_inputs(instance_from, tab_ids, instance_to)
    )
    cs_from = registry.get(instance_from)
    cs_to = registry.get(instance_to)
    session_from = cs_from.session_id if cs_from is not None else db_session_from
    session_to = cs_to.session_id if cs_to is not None else db_session_to

    # Source-session pre-check for the WHOLE call (as the single form does): a restarted
    # source means every tab_id is from a dead epoch — refuse the batch, touch nothing.
    if expected_session_from is not None and expected_session_from != session_from:
        raise ToolError(
            protocol.ERR_STALE_SESSION,
            f"source session for {instance_from} is not {expected_session_from!r} "
            "(the source browser restarted); bulk relocation refused",
        )

    now = _now_ms()
    results: list = [None] * len(tab_ids)
    plan: list = []  # (orig_index, tab_row) for items that will get a copy opened
    seen_urls: set = set()  # urls already kept in THIS batch (within-batch dedup)
    for i, tid in enumerate(tab_ids):
        tab = tabs_by_id.get(tid)
        if tab is None:
            results[i] = {"index": i, "ok": False, "error": "no_such_tab", "tab_id": tid}
            continue
        # #48 guard 4, per item: a pinned/audible/active-in-focus source can never be
        # closed by phase B — do not open an un-closeable copy for it.
        if tab["pinned"]:
            results[i] = {"index": i, "ok": False, "error": protocol.ERR_PRECONDITION_FAILED,
                          "reason": "pinned", "tab_id": tid}
            continue
        if tab["audible"]:
            results[i] = {"index": i, "ok": False, "error": protocol.ERR_PRECONDITION_FAILED,
                          "reason": "audible", "tab_id": tid}
            continue
        if (tab["active"] and tab["window_id"] is not None
                and tab["window_id"] == focused_window_id_from):
            results[i] = {"index": i, "ok": False, "error": protocol.ERR_PRECONDITION_FAILED,
                          "reason": "active_in_focus", "tab_id": tid}
            continue
        url = tab["url"]
        if url in target_urls or url in seen_urls:
            results[i] = {"index": i, "ok": False, "error": "duplicate", "tab_id": tid}
            continue
        seen_urls.add(url)
        plan.append((i, tab))

    if not plan:  # everything failed a guard or deduped — no pass, no rows.
        return {"ok": True, "undo_pass_id": None, "results": results}

    pass_id = f"mcp-{uuid4()}"

    # Phase A: ONE open_tab {items} frame to the target. A frame-level failure
    # (no_connection/timeout) raises here with nothing written — UNKNOWN, agent re-reads.
    open_items = [
        {
            "url": tab["url"], "pinned": False, "active": False,
            "seed_age_ms": now - tab["last_active_at"],
            "seed_opened_ago_ms": now - tab["opened_at"],
            "seed_age_unknown": bool(tab["age_unknown"]),
        }
        for (_i, tab) in plan
    ]
    open_result = await _command(
        app, instance_to, protocol.CMD_OPEN_TAB, {"items": open_items}, auth_ctx=auth_ctx
    )
    opened = _reconcile_bulk_results(open_result.get("results"), open_items)

    live: list = []  # (orig_index, tab, tab_id_to, window_id_to)
    for j, (i, tab) in enumerate(plan):
        r = opened[j]
        tid_to = r.get("tabId")
        if not r.get("ok") or not isinstance(tid_to, int) or isinstance(tid_to, bool):
            results[i] = {"index": i, "ok": False, "error": r.get("error") or "open_failed",
                          "message": r.get("message"), "tab_id": tab["tab_id"]}
            continue
        live.append((i, tab, tid_to, r.get("windowId")))

    if not live:  # every copy failed to open — nothing to close, no rows to write.
        return {"ok": True, "undo_pass_id": None, "results": results}

    # Steps 3+4 for every live item in ONE transaction: the copy's mirror row + the live
    # ``relocate``(done) + the ``relocate_close``(pending) marker (the at-least-once
    # discipline of #48, all under the shared pass_id).
    def _write_pairs(conn):
        from src.curator.phases import _insert_copy_tab

        out: dict = {}
        for (i, tab, tid_to, win_to) in live:
            seed = SimpleNamespace(
                url=tab["url"], title=tab["title"], opened_at=tab["opened_at"],
                last_active_at=tab["last_active_at"], age_unknown=tab["age_unknown"],
            )
            _insert_copy_tab(
                conn, instance_id=instance_to, tab_id=tid_to, window_id=win_to,
                tab=seed, now=now,
            )
            url = tab["url"]
            url_norm = normalize_url(url)
            relocate_id = insert_action(
                conn, ts=now, kind="relocate", status="done", initiator="mcp",
                pass_id=pass_id, instance_from=instance_from, instance_to=instance_to,
                tab_id=tab["tab_id"], session_id_from=session_from,
                tab_id_to=tid_to, session_id_to=session_to, decision="mcp_relocate",
                src_opened_at=tab["opened_at"], src_last_active_at=tab["last_active_at"],
                src_age_unknown=tab["age_unknown"], url=url, url_norm=url_norm,
                title=tab["title"], pinned=0,
            )
            close_id = insert_action(
                conn, ts=now, kind="relocate_close", status="pending", initiator="mcp",
                pass_id=pass_id, origin_action_id=relocate_id,
                instance_from=instance_from, instance_to=instance_to,
                tab_id=tab["tab_id"], session_id_from=session_from,
                tab_id_to=tid_to, session_id_to=session_to,
                url=url, url_norm=url_norm, title=tab["title"],
            )
            out[i] = (relocate_id, close_id)
        return out

    pair_ids = await db.write(_write_pairs)

    def _base(i, tab, tid_to):
        return {"index": i, "ok": True, "tab_id_to": tid_to, "instance_to": instance_to,
                "undo_pass_id": pass_id, "tab_id": tab["tab_id"]}

    # COPY CHECK (#48's guard, per item, over the batch): before closing any source,
    # confirm every copy still exists on the target. A target restart between phase A and
    # now would have destroyed the copies; the phase B close still matches
    # expect{url,notAudible,notPinned} and would delete the sources — an IRRECOVERABLE loss
    # with no pending row left to reconcile. ONE get_tab {items} to the target, NO
    # ``expected_session`` (reading a restarted target is fine, as #48 single does). This
    # makes bulk relocate 3 frames (open, get_tab, close) — consistent with #48's 3 commands;
    # correctness over the 2-frame budget.
    check_items = [{"tabId": tid_to} for (_i, _tab, tid_to, _win) in live]
    try:
        check_result = await _command(
            app, instance_to, protocol.CMD_GET_TAB, {"items": check_items}, auth_ctx=auth_ctx,
        )
    except ToolError as exc:
        # Frame-level failure (no_connection / timeout to the target) — UNKNOWN for the whole
        # frame. Sources untouched, ``pending`` rows left; mark every live item ``half`` and
        # return (mirror of the close-frame-failure branch below).
        for (i, tab, tid_to, _win) in live:
            results[i] = {**_base(i, tab, tid_to), "status": "half", "reason": exc.code}
        return {"ok": True, "undo_pass_id": pass_id, "results": results}

    checked = _reconcile_bulk_results(check_result.get("results"), check_items)
    present: list = []  # (orig_index, tab, tab_id_to, window_id_to) whose copy is confirmed
    for j, (i, tab, tid_to, win_to) in enumerate(live):
        if checked[j].get("ok"):
            present.append((i, tab, tid_to, win_to))
        else:
            # The copy vanished (target restart): degrade to half/copy_gone, DO NOT close the
            # source, and LEAVE its relocate_close pending — reconcile finds the source present
            # and abandons it. Only confirmed copies proceed to phase B.
            results[i] = {**_base(i, tab, tid_to), "status": "half", "reason": "copy_gone"}

    if not present:  # every copy vanished — no source to close, pending rows left for reconcile.
        return {"ok": True, "undo_pass_id": pass_id, "results": results}

    # Phase B: ONE close_tab {items} frame to the SOURCE, stamped with the pinned #47
    # epoch. The step-4 guards (url/notAudible/notPinned, deliberately NO minIdleMs — the
    # agent explicitly chose these tabs) ride per item.
    close_items = []
    close_map = []  # (orig_index, tab, close_id, tab_id_to)
    for (i, tab, tid_to, _win_to) in present:
        close_items.append({
            "tabId": tab["tab_id"],
            "expect": {"url": tab["url"], "notAudible": True, "notPinned": True},
        })
        close_map.append((i, tab, pair_ids[i][1], tid_to))

    try:
        close_result = await _command(
            app, instance_from, protocol.CMD_CLOSE_TAB, {"items": close_items},
            auth_ctx=auth_ctx, expected_session=expected_session_from,
        )
    except ToolError as exc:
        # Frame-level failure (no_connection / timeout / stale_session): the source close
        # is UNKNOWN for the whole frame. Leave the ``pending`` rows for the pass's
        # reconcile and report every live item ``half`` with the code.
        for (i, tab, _cid, tid_to) in close_map:
            results[i] = {**_base(i, tab, tid_to), "status": "half", "reason": exc.code}
        return {"ok": True, "undo_pass_id": pass_id, "results": results}

    closed = _reconcile_bulk_results(close_result.get("results"), close_items)

    # Resolve each live item and apply ALL pending-row transitions in one transaction.
    resolutions: list = []  # ("done"|"done_gone"|"failed", close_id[, iid, tab_id][, reason])
    for k, (i, tab, close_id, tid_to) in enumerate(close_map):
        cr = closed[k]
        if cr.get("ok"):
            resolutions.append(("done", close_id, instance_from, tab["tab_id"]))
            results[i] = {**_base(i, tab, tid_to), "status": "done"}
        elif cr.get("error") == protocol.ERR_NO_SUCH_TAB:
            # The source is already gone: the goal is reached — complete it.
            resolutions.append(("done_gone", close_id, instance_from, tab["tab_id"]))
            results[i] = {**_base(i, tab, tid_to), "status": "done"}
        elif cr.get("error") == protocol.ERR_PRECONDITION_FAILED:
            resolutions.append(("failed", close_id, protocol.ERR_PRECONDITION_FAILED))
            results[i] = {**_base(i, tab, tid_to), "status": "half",
                          "reason": protocol.ERR_PRECONDITION_FAILED}
        else:  # no_result / connection-class / any other UNKNOWN per-item code: uncertain.
            # Report ``half`` but LEAVE the row ``pending`` (append NO resolution) — reconcile
            # checks the mirror. Never ``failed``, which a later pass would blindly retry; this
            # mirrors #48 single's UNKNOWN/connection-class close.
            reason = cr.get("error") or "no_result"
            results[i] = {**_base(i, tab, tid_to), "status": "half", "reason": reason}

    def _apply(conn):
        for res in resolutions:
            if res[0] == "done":
                set_action_status(conn, res[1], "done")
                _delete_source_tab(conn, res[2], res[3])
            elif res[0] == "done_gone":
                set_action_status(conn, res[1], "done", reason=protocol.ERR_NO_SUCH_TAB)
                _delete_source_tab(conn, res[2], res[3])
            else:  # failed
                set_action_status(conn, res[1], "failed", reason=res[2])

    await db.write(_apply)
    return {"ok": True, "undo_pass_id": pass_id, "results": results}


def _delete_source_tab(conn, instance_id: str, tab_id: int) -> None:
    """Drop the closed source tab from the mirror so the verb's own view is consistent
    at once (the next snapshot would remove it anyway). Mirrors the pass's ``_delete_tab``.
    """
    conn.execute(
        "DELETE FROM tabs WHERE instance_id = ? AND tab_id = ?", (instance_id, tab_id)
    )


# --- exemptions: the agent's «не трогать» lease (§10/§11) --------------------
#
# The ``exemptions`` table and the pass's respect for it are OLD (``step4_passes`` skips a
# tab whose instance+url carries a row with ``until`` in the future). What was missing is a
# WRITER the agent can reach: only ``restore`` and ``/api/exemptions`` wrote rows, so a tab
# an agent opened for a task could be collapsed by the very next pass, mid-task.
#
# Every rule below is IMPORTED from :mod:`src.api.exemptions`, never restated: the same
# normalization, the same http(s)-with-a-host url check (a scheme-less url would produce a
# row that can never match a live tab — a permanently invisible no-op), the same known-
# instance check, and the same «never infinite» 30-day ceiling. A second door that
# re-derived those rules is exactly how a ceiling gets quietly weakened on one side.
async def list_exemptions(app, *, instance: str | None = None,
                          include_expired: bool = False) -> dict:
    """Active exemptions, optionally for ONE instance; ``include_expired`` also lists lapsed
    rows (they are not deleted eagerly — the pass just ignores them — which is what makes
    "it WAS protected until 14:20" answerable afterwards).

    The ``instance`` filter is applied over the rows rather than in SQL: the reader is
    shared with the HTTP endpoint and the table is small (one row per protected url), so
    narrowing it here costs nothing and keeps ONE query shape."""
    now = _now_ms()
    items = await app.state.db.read(
        lambda c: exemptions_api._list(c, now, include_expired)
    )
    if instance is not None:
        items = [i for i in items if i["instance_id"] == instance]
    return {"ok": True, "server_now": now, "exemptions": items}


async def set_exemption(app, *, instance: str, url: str, ttl_s: int,
                        reason: str | None = None) -> dict:
    """Protect ``instance`` + ``url`` from the pass for ``ttl_s`` seconds (refreshes an
    existing row — the table's PK is the pair, so a repeat call moves the deadline instead
    of growing duplicates).

    Gated by the stop like every other mutating verb (§12), and by the shared ceiling: a
    ttl above 30 days is clamped, never honoured. There is deliberately no "forever"."""
    await _ensure_not_paused(app)
    try:
        req = _req(app)
        instance_id = await exemptions_api._validate_instance(req, instance)
        target = exemptions_api._validate_url(url)
        now = _now_ms()
        until = exemptions_api.until_from_ttl_s(now, ttl_s)
    except HTTPException as exc:
        raise ToolError(*_tool_error_from_http(exc))
    label = reason if isinstance(reason, str) and reason else "mcp"
    await app.state.db.write(
        lambda c: exemptions_api._upsert(c, instance_id, target, until, label)
    )
    return {
        "ok": True,
        "exemption": {
            "instance_id": instance_id, "url": target,
            "url_norm": normalize_url(target), "until": until,
            "reason": label, "expired": False,
        },
    }


async def clear_exemption(app, *, instance: str, url: str) -> dict:
    """Lift an exemption. Idempotent: ``deleted: 0`` when it was already gone."""
    await _ensure_not_paused(app)
    try:
        req = _req(app)
        instance_id = await exemptions_api._validate_instance(req, instance)
        target = exemptions_api._validate_url(url)
    except HTTPException as exc:
        raise ToolError(*_tool_error_from_http(exc))
    deleted = await app.state.db.write(
        lambda c: exemptions_api._delete(c, instance_id, target)
    )
    return {"ok": True, "deleted": deleted}


def _plan_open_lease(url: str, ttl_s: int) -> tuple[str, int]:
    """Validate ``open_tab``'s lease ARGUMENTS and return ``(url, until)``. Raises.

    Called BEFORE the tab is opened. The rules are the shared ones — the same url check
    and the same 30-day ceiling ``set_exemption`` applies — so the identical ``ttl_s``
    cannot be a refusal through one verb and a shrug through another."""
    try:
        return (
            exemptions_api._validate_url(url),
            exemptions_api.until_from_ttl_s(_now_ms(), ttl_s),
        )
    except HTTPException as exc:
        raise ToolError(*_tool_error_from_http(exc))


async def _write_open_lease(app, instance: str, url: str, until: int) -> dict:
    """Best-effort exemption WRITE for a tab ``open_tab`` just opened — the "owned by the
    agent" lease. Arguments were already validated by :func:`_plan_open_lease`.

    NEVER raises: the tab IS open by the time this runs, so turning a failed write into a
    failed ``open_tab`` would report "nothing happened" about a tab that exists and invite
    the agent to open a second one. The outcome is REPORTED instead, and an agent that
    needs the protection can see it did not get it. What remains here is genuinely a
    RUNTIME fault (a degraded DB, a disk full) — never a bad argument, which is refused
    before the browser is touched at all."""
    try:
        await app.state.db.write(
            lambda c: exemptions_api._upsert(c, instance, url, until, "mcp_lease")
        )
        return {"ok": True, "until": until, "reason": "mcp_lease"}
    except Exception as exc:  # noqa: BLE001 - a lease fault must not mask an open tab
        return {"ok": False, "error": "lease_write_failed", "message": str(exc)}


# --- pass + stop/start -------------------------------------------------------
async def run_pass(app, *, dry_run: bool = False) -> dict:
    """Trigger one curator pass (§7). Delegates to the runner, which itself honours the
    stop at step 1 (and never mutes a dry_run — the plan is exactly why one stops)."""
    app_state = app.state
    return await runner.run_pass(
        app_state.db, app_state.ext_registry, app_state.settings,
        dry_run=bool(dry_run),
        clock_guard=getattr(app_state, "curator_clock", None),
    )


async def pause(app, *, minutes: int | None = None) -> dict:
    """Stop the curator INDEFINITELY: write ``curator_stopped_at`` + bump the fencing
    epoch (fences an in-flight pass). Not itself a "mutating verb" the stop refuses.

    The tool KEEPS its historical name (``pause``) for API stability, but there is no
    duration anymore: the stop lasts until ``resume``. ``minutes`` is accepted for
    backward compatibility with older agent configs and IGNORED — the same write-shape
    as ``POST /api/pause``, which likewise ignores its former body."""
    del minutes  # accepted for compatibility, deliberately ignored (the stop is indefinite)
    now = _now_ms()
    stopped_at = await app.state.db.write(lambda c: pause_ops.stop(c, now=now))
    return {"ok": True, "stopped_at": stopped_at}


async def resume(app) -> dict:
    """Start the curator (§7/§12): apply the TTL shift on the ACTUAL stop duration,
    clear ``curator_stopped_at``, THEN run a pass immediately — the SAME
    :func:`src.api.pause.resume_now` core ``DELETE /api/pause`` runs. §7 makes the
    immediate pass part of what "start by hand" MEANS; stopping after the settings
    write left the same verb with two behaviours depending on which door it was called
    through, and an agent's resume left the curator idle until the next tick.

    Whether that pass CONFIRMS the over-threshold plan latch (``resume_pending``)
    follows ``resume_now``'s two meanings of the one verb: a resume that lifts an
    ACTUAL stop runs the NORMAL threshold gate — an over-threshold plan recomputed at
    start time latches and is returned, never silently executed, because the stop
    hid the plan from whoever pressed start. A resume with NO stop armed is the
    informed confirm: it executes the plan recomputed at click time, so it doubles
    as the agent's "yes, run the big plan" verb. With nothing armed at all the
    confirm degrades to an ordinary pass inside the runner.

    KNOWN COST, accepted deliberately: this BLOCKS for the whole pass — tens of
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
