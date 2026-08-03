"""``/admin`` HTML console — the first HTML surface of the service (issue #36).

This is the presentation layer over the #35 ``/admin/*`` JSON API. It serves, via EXPLICIT
handlers (never ``StaticFiles`` — that would not let us set the CSP header), the console
page, the login form, and their ``.js`` / ``.css`` assets, plus the cookie-session
login/logout endpoints. The page renders pending enroll requests, instances and the
enrollment window entirely through SAME-ORIGIN ``fetch`` calls against the JSON API, so no
data is templated into the HTML — the assets are static and contain NO secrets, hence they
are served WITHOUT the auth guard.

Security posture:

* **CSP by explicit header** (:data:`_CSP`) on every HTML/asset response: ``default-src
  'none'`` with ``script-src 'self'`` — so there is NO inline ``<script>``; the page logic
  lives in the separately-served ``/admin/app.js``. This is why ``StaticFiles`` is unusable
  here: it cannot attach the policy.
* **Session cookie = a random id, never the ADMIN_TOKEN** (see :mod:`src.api.admin_session`).
  ``HttpOnly`` + ``Secure`` (UNCONDITIONAL — TLS terminates at Traefik, so the app sees
  ``http`` and deriving ``Secure`` from the request scheme would silently drop it in prod) +
  ``SameSite=Strict`` + ``Path=/admin``.
* **``GET /admin`` is behind the auth guard** (:func:`src.api.admin.require_admin`) — an
  unauthenticated hit never receives the console, only a 303 to the public login form
  (acc 1); the JSON API keeps answering a flat 401. The login/asset routes are public.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from src.api import admin_session
from src.api.admin import require_admin
from src.api.auth_metrics import auth_rejections
from src.api.guards import (
    MAX_UNAUTHENTICATED_BODY_BYTES,
    _bearer_token,
    read_bounded_body,
    require_same_origin,
)

# Explicit Content-Security-Policy for every /admin HTML/asset response. `default-src
# 'none'` denies everything by default; `script-src 'self'` / `style-src 'self'` permit only
# same-origin assets (so NO inline <script>/<style>); `connect-src 'self'` scopes the page's
# fetch() to this origin; `base-uri 'none'` / `form-action 'self'` blunt base-tag and form
# hijacks; `img-src 'self'` bars remote pixels. `frame-ancestors 'none'` forbids ANY site
# from framing the console — it does NOT inherit from default-src, so it must be stated
# explicitly; without it a same-site Traefik neighbor could iframe the authenticated console
# and clickjack the operator into same-origin (thus CSRF-passing) Approve/Revoke fetches.
# One string, applied identically everywhere.
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)

# templates/ ships inside the image (Dockerfile copies it). This module is src/api/…, so the
# repo root — and templates/ under it — is two parents up. Resolved once.
_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

# The ``curator_auth_rejections_total{reason}`` label for a FAILED ADMIN_TOKEN login — the
# brute-force signal, kept apart from the ambient ``admin_session`` (see login_submit).
# deploy/alerts.yml keys ``curator-admin-token-bruteforce`` on this exact string.
ADMIN_BAD_TOKEN_REASON = "admin_bad_token"


class AdminSecurityHeadersMiddleware:
    """Stamp ``X-Content-Type-Options: nosniff`` and ``Cache-Control: no-store`` on EVERY
    ``/admin`` response.

    Two cross-cutting headers covering both the HTML/asset responses and the #35 JSON API
    responses. Implemented as a pure-ASGI middleware so it adds ONLY headers and never
    touches the #35 JSON bodies or status codes. Frame defenses (CSP ``frame-ancestors`` +
    ``X-Frame-Options``) stay on the HTML/asset responses themselves, where framing is the
    threat.

    * ``nosniff`` — a browser MIME-sniffing a JSON body into active HTML must not be
      possible. The bodies carry no free-text client input anymore (the pending list that
      served ``suggested_title`` / ``origin`` verbatim is gone, and an instance id is
      charset-bounded before the row exists), but the header stays: it is what keeps the
      NEXT field from being the hole, and it costs nothing.
    * ``no-store`` — ``/admin`` is entirely secrets: ``GET /admin/enroll/window`` returns
      the LIVE window code, which is now the WHOLE permission to enrol, and the console
      page is only meaningful to an authenticated operator. None of it carried any cache
      directive, so the default heuristics let a browser (or any intermediary) write the
      live code to the disk cache, where it outlives both the window and the session.
      ``no-store`` is the only directive that forbids writing it down at all —
      ``no-cache`` still permits a stored copy.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/admin" or path.startswith("/admin/")):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                # setdefault: never duplicate a header an inner response already set.
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("cache-control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


@lru_cache(maxsize=None)
def _asset_bytes(name: str) -> bytes:
    """Read a template asset once and cache it (the files are immutable at runtime)."""
    return (_TEMPLATES_DIR / name).read_bytes()


def _asset_response(name: str, media_type: str) -> Response:
    """A static asset/HTML response carrying the framing-defense headers. NOT auth-gated
    (no secrets). ``X-Frame-Options: DENY`` backs up ``frame-ancestors 'none'`` for any
    client that predates CSP framing directives — belt-and-suspenders against clickjacking.
    ``X-Content-Type-Options: nosniff`` is added for every /admin response (JSON included) by
    :class:`AdminSecurityHeadersMiddleware`, so it is not repeated here.
    """
    return Response(
        content=_asset_bytes(name),
        media_type=media_type,
        headers={
            "Content-Security-Policy": _CSP,
            "X-Frame-Options": "DENY",
        },
    )


def _cookie_ttl_seconds(request: Request) -> int:
    return request.app.state.settings.admin_session_ttl_min * 60


def _set_session_cookie(response: Response, session_id: str, max_age: int) -> None:
    """Set the session cookie with the fixed hardening flags (see module docstring).

    ``Secure`` is UNCONDITIONAL on purpose: the app sees ``http`` behind Traefik, so
    deriving it from the request scheme would silently drop it in production.
    """
    response.set_cookie(
        key=admin_session.COOKIE_NAME,
        value=session_id,
        max_age=max_age,
        path="/admin",
        httponly=True,
        secure=True,
        samesite="strict",
    )


# Where a browser that failed the guard on GET /admin is sent. It is the PUBLIC route
# (`login_page` below, no `require_admin`), which is what makes the redirect terminal: the
# target cannot refuse and bounce back here, so no loop is constructible.
_LOGIN_PATH = "/admin/login"


# --- HTML pages --------------------------------------------------------------
async def admin_page(request: Request) -> Response:
    """``GET /admin`` — the console page (auth-gated read; degraded-OK).

    Requires a valid cookie OR Bearer ADMIN_TOKEN via :func:`require_admin`. No
    ``require_operational`` — the console must render (reads) even in degraded mode (acc 8).
    The HTML itself holds no data: it fetches the JSON API on load.

    **An unauthenticated hit is redirected to the login form, not answered 401** (acc 1,
    amended). The gate itself is untouched — the console body is still served ONLY to an
    authenticated caller — but the REFUSAL is presented differently on this one route,
    because this is the only ``/admin`` surface a human reaches by typing an address. The
    401 was correct and useless: it put the bare text ``admin authentication required`` in
    front of an operator whose only mistake was not having a cookie yet, while the login
    form that fixes it sat one public URL away, reachable only from ``app.js`` — i.e. only
    for a console that had ALREADY loaded. Two surfaces, two right answers: a program gets
    a status it can branch on, a person gets the place to type the password.

    The redirect is deliberately narrow, because "redirect instead of 401" is a footgun
    anywhere else:

    * **HTML route only.** Every ``/admin/*`` JSON route keeps its flat 401 (:func:`
      require_admin` is unchanged). Redirecting an API call would swap a machine-readable
      refusal for an HTML page — a client that follows redirects by default (``fetch``
      does) would parse the login form as its payload.
    * **No-credential branch only.** A request that DID present ``Authorization: Bearer``
      and was rejected keeps the 401: that caller is curl/MCP/the agent, not a browser, and
      it also keeps #35 acc 6 intact — an instance secret probing ``/admin`` still gets the
      same 401 as an unknown token, with no page to distinguish them by.
    * **401 only.** A 503 (degraded resolve) or 403 (CSRF) propagates untouched; sending a
      DB outage to a login form would hide it.

    ``303 See Other``: the request is a GET whose answer lives at a different URI. 303 says
    exactly that and rewrites the method to GET on any future use, where 302 is the
    historically ambiguous one and 307 preserves a method this GET-only route has no use
    for; 301/308 would additionally invite a client to REMEMBER the substitution, which is
    wrong the moment the operator logs in. (``Cache-Control: no-store`` from
    :class:`AdminSecurityHeadersMiddleware` covers the redirect too.)

    The rejection is still counted: ``require_admin`` has already ticked
    ``curator_auth_rejections_total{reason="admin_session"}`` by the time the exception
    reaches here, and that stays. ``admin_session`` is the AMBIENT label by design — it
    ticks once per browser that opens the console before logging in, carries no alert rule
    (deploy/alerts.yml, and the exclusion is pinned in tests/test_deploy_alerts.py), and is
    precisely the label the brute-force rule was split AWAY from. So a human walking into
    the login form neither inflates ``admin_bad_token`` (only a WRONG token POSTed to
    ``login_submit`` does) nor pages anyone — while the series itself stays alive, so
    "the console is being hit without a session" remains observable.
    """
    try:
        await require_admin(request)
    except HTTPException as exc:
        if exc.status_code != 401 or _bearer_token(request) is not None:
            raise
        return RedirectResponse(_LOGIN_PATH, status_code=303)
    return _asset_response("admin.html", "text/html; charset=utf-8")


async def login_page(request: Request) -> Response:
    """``GET /admin/login`` — the login form. PUBLIC (holds no secret) + CSP."""
    return _asset_response("login.html", "text/html; charset=utf-8")


# --- static assets (public, CSP) ---------------------------------------------
async def app_js(request: Request) -> Response:
    return _asset_response("app.js", "text/javascript; charset=utf-8")


async def app_css(request: Request) -> Response:
    return _asset_response("app.css", "text/css; charset=utf-8")


async def login_js(request: Request) -> Response:
    return _asset_response("login.js", "text/javascript; charset=utf-8")


# --- login / logout ----------------------------------------------------------
async def _submitted_token(request: Request) -> str:
    """Read the token from a form POST (the login form) or a JSON body (curl/tests).

    The body is read through :func:`read_bounded_body` FIRST (413 past
    :data:`MAX_UNAUTHENTICATED_BODY_BYTES`). This is the service's only pre-auth body:
    ``login_submit`` is deliberately not behind ``require_admin`` — it is how one
    authenticates — so an anonymous peer decides how much this endpoint buffers. The cap
    runs before the JSON/form parser, which is what makes it a cap and not a post-mortem.
    """
    await read_bounded_body(request, MAX_UNAUTHENTICATED_BODY_BYTES)
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except Exception:
            return ""
        token = data.get("token") if isinstance(data, dict) else None
        return token if isinstance(token, str) else ""
    try:
        form = await request.form()
    except Exception:
        # A malformed/undecodable form body is simply "no token" — never a 500 on a
        # surface anybody may POST to.
        return ""
    token = form.get("token")
    return token if isinstance(token, str) else ""


async def login_submit(request: Request) -> Response:
    """``POST /admin/login`` — verify ADMIN_TOKEN → mint a cookie session (acc 2).

    The submitted token is compared CONSTANT-TIME against ``settings.admin_token``; on a
    match a random session id is minted (NEVER the token) and set as the hardened cookie,
    and the endpoint answers 200. A wrong/empty token is a flat 401. Not behind
    ``require_admin`` (this is how you AUTHENTICATE) and not behind ``require_operational``
    (you may log in to read while degraded); it touches only the in-memory store.

    A failure is counted under its OWN reason, :data:`ADMIN_BAD_TOKEN_REASON` — never the
    generic ``admin_session`` that ``require_admin`` uses. The two events are nothing
    alike: "someone opened /admin without a session" happens every time a browser hits the
    console before logging in and is pure background noise, while "someone POSTed a WRONG
    ADMIN_TOKEN" is a guess at the credential that opens /admin, /api/* and /mcp. Folded
    into one label the second is unalertable — any threshold that survives the first is far
    above a real brute force. Split, ``curator-admin-token-bruteforce`` can key on this
    one (deploy/alerts.yml).
    """
    submitted = await _submitted_token(request)
    expected = request.app.state.settings.admin_token
    if not secrets.compare_digest(
        submitted.encode("utf-8", "ignore"), expected.encode("utf-8")
    ):
        auth_rejections.incr(ADMIN_BAD_TOKEN_REASON)
        raise HTTPException(status_code=401, detail="invalid admin token")

    session_id = admin_session.create(request.app)
    response = JSONResponse({"ok": True})
    _set_session_cookie(response, session_id, _cookie_ttl_seconds(request))
    return response


async def logout_submit(request: Request) -> Response:
    """``POST /admin/logout`` — end the presented session + clear the cookie (acc 5).

    Deletes whatever session id the cookie carries from the in-memory store (idempotent)
    and clears the cookie. It is not behind ``require_admin`` (harmless — it only drops the
    presented id), but it IS a cookie-path MUTATING verb, so it takes the SAME same-origin
    CSRF gate: a same-site neighbor must not be able to force a logout (forced-logout DoS)
    with the ambient cookie.
    """
    require_same_origin(request)
    session_id = request.cookies.get(admin_session.COOKIE_NAME)
    admin_session.delete(request.app, session_id)
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=admin_session.COOKIE_NAME, path="/admin")
    return response
