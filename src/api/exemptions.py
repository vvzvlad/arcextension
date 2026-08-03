"""``GET/POST/DELETE /api/exemptions`` — the «не трогать до …» protection (§10).

The ``exemptions`` table (§4) has existed since the schema landed and the pass has
always honoured it (:func:`src.curator.decide.step4_passes` skips a tab whose
instance+URL carries a row with ``until`` in the future), but the only writer was
``restore`` — the human had no way to say "leave this tab alone until tonight", and
no way to see or lift the ones restore had written. §10's endpoint table lists
``GET/POST/DELETE /api/exemptions`` for exactly that.

Shape follows the table and how the pass reads it:

* keyed by ``(instance_id, url)`` — the table's PRIMARY KEY, so a repeat POST for the
  same pair REFRESHES the deadline instead of growing duplicates;
* matched by NORMALIZED url (query/fragment stripped) when the pass compares, so the
  exact string stored here need not be byte-identical to the live tab's address;
* ``until`` is absolute server-clock ms; ``reason`` is free text (``restore`` for the
  rows restore writes, ``manual`` for these).

Guards are the ``/api/*`` standard: ``require_api_caller`` (Bearer ADMIN_TOKEN or an
instance secret — §35 §4), ``require_operational``, and
the pause gate on the two mutating verbs. There is deliberately no ``force`` here: §7
grants that escape to the human's own BUTTONS (focus / restore / undo), and editing the
protection policy while the emergency stop is pulled is not one of them.
"""

from __future__ import annotations

import sqlite3
import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_api_caller, require_not_paused, require_operational
from src.db.actions import normalize_url
from src.rules import access as rules_access
from src.rules.matcher import normalize_target

# A manual exemption is never infinite: an unbounded "do not touch" row would quietly
# retire a URL from curation forever, which is the same failure the finite pause exists
# to prevent (§7). 30 days is a generous ceiling that still guarantees self-expiry.
MAX_MINUTES = 30 * 24 * 60


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- readers / writers (sync ``fn(conn)``, one transaction each) -------------
def _list(conn: sqlite3.Connection, now: int, include_expired: bool) -> list[dict]:
    conn.row_factory = sqlite3.Row
    sql = "SELECT instance_id, url, until, reason FROM exemptions"
    args: tuple = ()
    if not include_expired:
        sql += " WHERE until > ?"
        args = (now,)
    sql += " ORDER BY until DESC, instance_id, url"
    return [
        {
            "instance_id": r["instance_id"],
            "url": r["url"],
            "url_norm": normalize_url(r["url"]),
            "until": r["until"],
            "reason": r["reason"],
            "expired": r["until"] <= now,
        }
        for r in conn.execute(sql, args).fetchall()
    ]


def _upsert(conn: sqlite3.Connection, instance_id: str, url: str, until: int, reason: str) -> None:
    # PK (instance_id, url) => a repeat POST refreshes the deadline (same discipline as
    # restore's ``_upsert_exemption``), never a second row for the same pair.
    conn.execute(
        "INSERT INTO exemptions (instance_id, url, until, reason) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(instance_id, url) DO UPDATE SET "
        "until = excluded.until, reason = excluded.reason",
        (instance_id, url, until, reason),
    )


def _delete(conn: sqlite3.Connection, instance_id: str, url: str) -> int:
    cur = conn.execute(
        "DELETE FROM exemptions WHERE instance_id = ? AND url = ?", (instance_id, url)
    )
    return int(cur.rowcount or 0)


# --- request parsing ---------------------------------------------------------
async def _json_object(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return data


def _flag(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes"}


async def _validate_instance(request: Request, instance_id) -> str:
    """A known instance id (or the configured main), else 422.

    Same reasoning as a rule's target (§12): an exemption on an instance that does not
    exist is a permanently invisible no-op — the pass matches on ``instance_id``, so a
    typo protects nothing and the human is never told.
    """
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise HTTPException(status_code=422, detail="instance_id is required")
    known = await request.app.state.db.read(rules_access.known_instance_ids)
    main = request.app.state.settings.main_instance_id
    if instance_id not in known and instance_id != main:
        raise HTTPException(
            status_code=422, detail=f"unknown instance_id {instance_id!r}"
        )
    return instance_id


def _validate_url(url) -> str:
    """A real http(s) URL with a host, else 422 — the same edge rule as ``open_tab``.

    Non-negotiable because of HOW the pass matches: it compares
    ``normalize_url(exemption.url) == normalize_url(tab.url)``, and ``normalize_url``
    is ``urlsplit``-based. A scheme-less ``grafana.lc/d/1`` — exactly what Chrome's
    address bar shows and a human copies — parses with an empty scheme and host, the
    whole string landing in ``path``, so it normalises to ``grafana.lc/d/1`` and can
    never equal a live tab's ``https://grafana.lc/d/1``.

    Accepting it would produce a 201, a row that ``GET /api/exemptions`` proudly lists as
    active, and a next pass that closes the very tab the human just protected: the
    "permanently invisible no-op" that :func:`_validate_instance` refuses for the same
    reason one field over. ``normalize_target`` is the check §12 already requires at the
    ``open_tab`` / ``navigate_tab`` edge, reused rather than re-derived.
    """
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=422, detail="url is required")
    if normalize_target(url) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"url {url!r} is not an http(s) URL with a host; an exemption is matched "
                "against the live tab's full address, so this row could never match "
                "anything (did you paste it without the scheme?)"
            ),
        )
    return url


def _resolve_until(body: dict, now: int) -> int:
    """``until`` (absolute ms) or ``minutes`` (relative), clamped finite and future.

    One of the two is REQUIRED: there is no sensible default horizon for "do not touch"
    to invent, and silently picking one would make the protection's length invisible.
    """
    until = body.get("until")
    minutes = body.get("minutes")
    if until is not None:
        if isinstance(until, bool) or not isinstance(until, int):
            raise HTTPException(status_code=422, detail="until must be an integer (ms)")
        if until <= now:
            raise HTTPException(status_code=422, detail="until must be in the future")
        cap = now + MAX_MINUTES * 60_000
        return min(until, cap)
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise HTTPException(status_code=422, detail="minutes must be an integer")
        if minutes < 1:
            raise HTTPException(status_code=422, detail="minutes must be >= 1")
        return now + min(minutes, MAX_MINUTES) * 60_000
    raise HTTPException(
        status_code=422, detail="one of `until` (ms) or `minutes` is required"
    )


# --- GET /api/exemptions -----------------------------------------------------
async def list_exemptions(request: Request) -> JSONResponse:
    """Active exemptions; ``?include_expired=1`` also returns the lapsed rows.

    Expired rows are not deleted eagerly (the pass simply ignores them), so the flag is
    what makes "it WAS protected until 14:20" answerable after the fact."""
    await require_api_caller(request)
    require_operational(request)
    include_expired = _flag(request.query_params.get("include_expired"))
    now = _now_ms()
    items = await request.app.state.db.read(lambda c: _list(c, now, include_expired))
    return JSONResponse({"server_now": now, "exemptions": items})


# --- POST /api/exemptions ----------------------------------------------------
async def create_exemption(request: Request) -> JSONResponse:
    """Create/refresh «не трогать <instance>+<url> до …»."""
    await require_api_caller(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused (§7); no force here

    body = await _json_object(request)
    instance_id = await _validate_instance(request, body.get("instance_id"))
    url = _validate_url(body.get("url"))
    now = _now_ms()
    until = _resolve_until(body, now)
    reason = body.get("reason") if isinstance(body.get("reason"), str) else "manual"

    await request.app.state.db.write(
        lambda c: _upsert(c, instance_id, url, until, reason or "manual")
    )
    return JSONResponse(
        {
            "ok": True,
            "exemption": {
                "instance_id": instance_id,
                "url": url,
                "url_norm": normalize_url(url),
                "until": until,
                "reason": reason or "manual",
                "expired": False,
            },
        },
        status_code=201,
    )


# --- DELETE /api/exemptions --------------------------------------------------
async def delete_exemption(request: Request) -> JSONResponse:
    """Lift an exemption. Idempotent: ``{"deleted": 0}`` when it was already gone.

    Accepts ``instance_id`` / ``url`` from a JSON body OR the query string — a DELETE
    with a body is legal but awkward for some clients, and the pair is short enough to
    ride in the query string."""
    await require_api_caller(request)
    require_operational(request)
    await require_not_paused(request)  # 423 while paused (§7); no force here

    body: dict = {}
    if await request.body():
        body = await _json_object(request)
    instance_id = body.get("instance_id") or request.query_params.get("instance_id")
    url = body.get("url") or request.query_params.get("url")
    instance_id = await _validate_instance(request, instance_id)
    url = _validate_url(url)

    deleted = await request.app.state.db.write(lambda c: _delete(c, instance_id, url))
    return JSONResponse({"ok": True, "deleted": deleted})
