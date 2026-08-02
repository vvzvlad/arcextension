"""``GET /api/actions`` — the archive list with filters (§10).

Bearer-authed, refuses degraded mode. Filters (all optional, combinable):

* ``kind`` — exact ``actions.kind``.
* ``pass_id`` — exact pass id.
* ``instance`` — matches either side of a move (``instance_from`` OR ``instance_to``).
* ``url`` — matched on ``url_norm`` (origin+path): the caller may pass a full URL and
  still find the row whatever tracking params it carried (the archive's search key).
* ``since`` — ``ts >= since`` (server-clock ms).
* ``limit`` / ``offset`` — pagination; ``limit`` is capped so one request cannot dump
  the whole archive.

``deferred`` rows (``status='deferred'`` — the aggregated "target not ready" markers,
§7) are HIDDEN by default and only returned with ``deferred=1`` (they are noise in the
human-facing archive). Returns ``{items, total}`` — ``total`` counts the SAME filtered
set (minus pagination) so a client can page. Newest-first. Every value is a bound SQL
parameter; nothing is string-interpolated.
"""

from __future__ import annotations

import sqlite3

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_ext_token, require_operational
from src.db.actions import normalize_url

# Columns surfaced in the archive list — enough to render a row and drive a restore,
# without dumping the whole decision basis on every list call.
_ITEM_COLUMNS = (
    "id",
    "pass_id",
    "ts",
    "kind",
    "status",
    "instance_from",
    "instance_to",
    "tab_id",
    "tab_id_to",
    "origin_action_id",
    "rule_id",
    "rule_pattern",
    "decision",
    "url",
    "url_norm",
    "title",
    "pinned",
    "reason",
    "initiator",
    "detail",
    "restored_at",
)

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


def _int_param(params, name: str, default: int, *, minimum: int, maximum: int | None) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{name} must be an integer")
    if val < minimum:
        val = minimum
    if maximum is not None and val > maximum:
        val = maximum
    return val


def _build_filters(params) -> tuple[str, list]:
    """Assemble a parameterized WHERE clause from the query filters (§10)."""
    where: list[str] = []
    args: list = []

    kind = params.get("kind")
    if kind:
        where.append("kind = ?")
        args.append(kind)

    pass_id = params.get("pass_id")
    if pass_id:
        where.append("pass_id = ?")
        args.append(pass_id)

    instance = params.get("instance")
    if instance:
        # A move touches two instances; match either side so "show me everything
        # involving <instance>" is one filter.
        where.append("(instance_from = ? OR instance_to = ?)")
        args.extend([instance, instance])

    url = params.get("url")
    if url:
        where.append("url_norm = ?")
        args.append(normalize_url(url))

    since = params.get("since")
    if since not in (None, ""):
        try:
            since_val = int(since)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="since must be an integer (ms)")
        where.append("ts >= ?")
        args.append(since_val)

    # deferred rows are HIDDEN by default; only an explicit opt-in returns them.
    if not _truthy(params.get("deferred")):
        where.append("status != 'deferred'")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, args


def _truthy(raw) -> bool:
    return str(raw).lower() in ("1", "true", "yes", "on") if raw is not None else False


def _list_actions(conn: sqlite3.Connection, clause: str, args: list, limit: int, offset: int):
    conn.row_factory = sqlite3.Row
    total = conn.execute(
        "SELECT COUNT(*) FROM actions" + clause, args
    ).fetchone()[0]
    # Newest-first; id breaks the ts tie so pagination is stable (§10).
    rows = conn.execute(
        "SELECT " + ", ".join(_ITEM_COLUMNS) + " FROM actions" + clause
        + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return int(total), [dict(r) for r in rows]


async def list_actions(request: Request) -> JSONResponse:
    require_ext_token(request)      # 401 before anything else
    require_operational(request)    # 503 in degraded mode

    params = request.query_params
    limit = _int_param(params, "limit", _DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT)
    offset = _int_param(params, "offset", 0, minimum=0, maximum=None)
    clause, args = _build_filters(params)

    total, items = await request.app.state.db.read(
        lambda c: _list_actions(c, clause, args, limit, offset)
    )
    return JSONResponse({"items": items, "total": total})
