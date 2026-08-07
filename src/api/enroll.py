"""``POST /api/enroll/window`` — arm the enrollment window from the STARTPAGE (§13).

The startpage has a one-click «Открыть регистрацию и скопировать код» button, and that
button needs the code IN THE PAGE: ``navigator.clipboard.writeText`` requires a secure
context AND the transient user activation of the clicking document, neither of which a
freshly opened ``/admin`` tab reliably has. So the code has to come back to the caller
that was clicked, not to a console the click merely opened.

It could not come from ``/admin/enroll/window``. The startpage authenticates to ``/api/*``
with the RAW INSTANCE SECRET (``startpage/src/lib/store.js`` init: ``token = cred.secret``),
and :func:`src.api.admin.require_admin` rejects an instance secret with 401 on purpose —
"the same status an unknown token gets, so an instance cannot probe /admin". That gate is
correct and stays. This module is therefore an ADDITIONAL, differently-authed entry point
onto the SAME core (:func:`src.curator.enroll.arm_enroll_window`), authed by
``require_api_caller`` (ADMIN_TOKEN **or** an active instance secret — §35 §4).
``POST/GET/DELETE /admin/enroll/window`` are untouched and stay ADMIN-only; the response
body here is deliberately byte-identical to the admin POST's, so the two verbs cannot
drift in meaning.

WHAT THIS COSTS. An enrolled instance can now arm the enrollment window. A stolen instance
secret therefore gains PERSISTENCE BEYOND A REVOKE: arm a window, read the code it returns,
and enroll a second identity under a different id — one that survives revocation of the
first. That is a real widening of what a leaked secret buys, and it is accepted, for five
reasons that hold together and not one of which carries it alone:

* under issue #35 §4 the ``/api`` instance caller IS the human at the keyboard — exactly
  what :func:`src.api.guards.initiator_for` encodes (instance → ``'user'``), and what the
  four force-verbs already act on;
* the window is short and self-closing — ``ENROLL_WINDOW_MIN`` (default 10 min), capped by
  ``ENROLL_WINDOW_MAX_MIN`` = 60 — and the code goes ONLY to the caller, never anywhere it
  can be read again;
* every arm writes an ``admin_audit`` row, and this verb records
  ``initiator=initiator_for(caller)``, so a startpage arm (``'user'``) is distinguishable
  in the trail from an admin/MCP arm (``'admin'``). That distinction is the POINT of using
  the helper here, not decoration — hard-coding ``"admin"`` would erase the one signal an
  operator has that a window was armed from a browser;
* a new instance shows up in the console's instance list, so the second identity is not
  invisible — it is a row an operator can see and revoke;
* the service is required to be unreachable from the public internet (``deploy/DEPLOY.md``
  §3), so the attacker who holds the secret is already inside the perimeter.

NO STOP GATE, deliberately. :func:`src.api.guards.require_not_paused` guards mutating
verbs because the emergency stop silences the curator's AUTOMATION; arming an enrollment
window is an operator action on the admin surface, not automation, and the existing
``/admin/enroll/window`` is not gated either — gating one and not the other would make the
same act legal or illegal depending on which door it came through.
``require_operational`` DOES apply: this is a write, and a degraded schema cannot be
trusted for authoritative writes (same as the admin handler).
"""

from __future__ import annotations

import json
import sqlite3
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.api.guards import initiator_for, require_api_caller, require_operational
from src.curator.enroll import arm_enroll_window
from src.db import queries


def _now_ms() -> int:
    return int(time.time() * 1000)


async def open_enroll_window_api(request: Request) -> JSONResponse:
    """``POST /api/enroll/window`` — arm the window; return ``{code, until, seconds_remaining}``.

    One click on the startpage does both halves of «открыть регистрацию»: the window is
    armed server-side and the freshly minted code travels back in the response, where the
    page's own click handler still holds the user activation the clipboard write needs.

    Body is ignored — the window has no parameters here. Its length is the configured
    ``ENROLL_WINDOW_MIN``, the same value the admin verb arms, so the two doors cannot
    open windows of different lengths. A FRESH code is minted on every open (slice A), so
    two consecutive arms never hand out the same code and a previous window's code is
    dead the moment this one is armed.

    Audited as ``window_open`` with ``initiator=initiator_for(caller)``: ``'user'`` for the
    startpage (an instance secret), ``'admin'`` for an ADMIN_TOKEN caller. See the module
    docstring for why that difference is load-bearing.
    """
    caller = await require_api_caller(request)
    require_operational(request)
    now = _now_ms()
    minutes = request.app.state.settings.enroll_window_min

    def _txn(c: sqlite3.Connection):
        state = arm_enroll_window(c, now=now, minutes=minutes)
        queries.insert_admin_audit(
            c, now=now, action="window_open", initiator=initiator_for(caller),
            detail=json.dumps({"until": state.until}),
        )
        return state

    state = await request.app.state.db.write(_txn)
    return JSONResponse(
        {"code": state.code, "until": state.until,
         "seconds_remaining": state.seconds_remaining}
    )
