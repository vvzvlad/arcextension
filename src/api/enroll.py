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
  ``ENROLL_WINDOW_MAX_MIN`` = 60 — and the code is minted fresh on every arm and is
  re-readable only through the ADMIN-only ``GET /admin/enroll/window``, and only until the
  deadline (which is why both that response and this one carry ``Cache-Control:
  no-store``). What the code DOES reach by design is the caller's own OS clipboard and the
  DOM of the tab that clicked, so "short" is the whole of its protection, not secrecy;
* every arm writes an ``admin_audit`` row, and this verb records
  ``initiator=initiator_for(caller)``, so a startpage arm (``'user'``) is distinguishable
  in the trail from an admin/MCP arm (``'admin'``). That distinction is the POINT of using
  the helper here, not decoration — hard-coding ``"admin"`` would erase the one signal an
  operator has that a window was armed from a browser;
* a new instance shows up in the console's instance list, so the second identity is not
  invisible — it is a row an operator can see and revoke;
* the service is required to be unreachable from the public internet (``deploy/DEPLOY.md``
  §3), so the attacker who holds the secret is already inside the perimeter.

One more cost is NAMED here rather than paid down:

* ``admin_audit`` is deliberately OUTSIDE retention (``src/db/retention.py`` sweeps only
  ``actions`` and ``js_audit``: a security trail is kept forever), and until this verb
  existed only an ADMIN caller could append to it. Any holder of an instance secret can
  now grow that table without bound — there is no rate limit anywhere in the service. No
  debounce and no cap is added on purpose: a FRESH code per open is load-bearing
  (:mod:`src.curator.enroll` — "код новый на каждое открытие окна"), so suppressing or
  coalescing an arm would either hand the caller a code an earlier screenshot still shows,
  or leave a clicked button with no code at all. The bound on this is the perimeter (the
  bullet above), and the rows are the operator's evidence, not noise to be dropped.

NO STOP GATE, deliberately. :func:`src.api.guards.require_not_paused` guards mutating
verbs because the emergency stop silences the curator's AUTOMATION; arming an enrollment
window is an operator action, not automation, so the stop has nothing to say about it.
Symmetry with ``/admin/enroll/window`` is NOT the argument — §7 lets that one through for
a reason that does not transfer: the same ``ADMIN_TOKEN`` already grants revoke, which is
strictly more power, so a gate there would protect nothing. An instance secret revokes
nothing, so this door has to stand on the operator-action reason alone.
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
    startpage (an instance secret), ``'admin'`` for an ADMIN_TOKEN caller — and with
    ``instance_id=caller.instance_id``, so the row names WHICH browser armed it (``None``
    for an ADMIN_TOKEN caller, which has no instance). Without the id an operator chasing a
    stray enrollment reads "some browser armed a window" and cannot tell whose secret
    leaked — which is exactly the "a new instance is visible in the console list" reason in
    the module docstring's trade-off list.
    """
    caller = await require_api_caller(request)
    require_operational(request)
    now = _now_ms()
    minutes = request.app.state.settings.enroll_window_min

    def _txn(c: sqlite3.Connection):
        state = arm_enroll_window(c, now=now, minutes=minutes)
        queries.insert_admin_audit(
            c, now=now, action="window_open", initiator=initiator_for(caller),
            instance_id=caller.instance_id,
            detail=json.dumps({"until": state.until}),
        )
        return state

    state = await request.app.state.db.write(_txn)
    # ``no-store`` is on the PAYLOAD, not on the method: this body carries the LIVE
    # enrollment window code, which IS the whole permission to enrol (§13), and the default
    # cache heuristics let a browser or an intermediary write it to disk, where it outlives
    # both the window and the session. ``AdminSecurityHeadersMiddleware`` puts the same
    # directive on ``GET /admin/enroll/window`` for exactly this reason, but it matches on
    # the ``/admin`` path prefix and this route is under ``/api``, so it never sees this
    # response — the header has to be set here.
    return JSONResponse(
        {"code": state.code, "until": state.until,
         "seconds_remaining": state.seconds_remaining},
        headers={"Cache-Control": "no-store"},
    )
