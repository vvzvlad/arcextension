"""``POST /api/quick_links/ops`` (§10).

The startpage owns an OFFLINE op queue in ``chrome.storage.local`` (offline can
last days, §10) and flushes it here as an ARRAY of ops with an ``Idempotency-Key``
header. Conflict resolution is last-write-wins by receive order; ``position`` is
recomputed SERVER-side (append); ``reorder`` is the explicit reposition op.

The whole batch — the idempotency check and every op — applies in ONE
``Database.write`` transaction (Фаза 2 contract: parameterized SQL, no ``await``
inside), so a retried flush can never half-apply. The response is the resulting
quick-links snapshot so the page can reconcile its optimistic cache.
"""

from __future__ import annotations

import time

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import require_api_caller, require_not_paused, require_operational
from src.db import quick_links as ql


def _now_ms() -> int:
    return int(time.time() * 1000)


async def quick_links_ops(request: Request) -> JSONResponse:
    await require_api_caller(request)
    require_operational(request)
    await require_not_paused(request)  # pause silences the offline op flush too (§7)

    try:
        ops = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")
    if not isinstance(ops, list):
        raise HTTPException(
            status_code=400, detail="request body must be a JSON array of ops"
        )

    # Idempotency-Key dedupes a retried flush of the SAME batch (§10). Optional: a
    # missing key just means "always apply" (a client that does not retry).
    key = request.headers.get("idempotency-key") or None

    quick_links = await request.app.state.db.write(
        lambda c: ql.apply_ops_with_key(c, ops, key, _now_ms())
    )
    return JSONResponse(
        {
            "ok": True,
            "quick_links": [
                {"id": r["id"], "url": r["url"], "title": r["title"], "position": r["position"]}
                for r in quick_links
            ],
        }
    )
