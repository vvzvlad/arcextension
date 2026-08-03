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
  unauthenticated hit is a 401 pointing at the login form, never a secret leak (acc 1). The
  login/asset routes are public.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.api import admin_session
from src.api.admin import require_admin
from src.api.auth_metrics import auth_rejections
from src.api.guards import require_same_origin

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


class AdminSecurityHeadersMiddleware:
    """Stamp ``X-Content-Type-Options: nosniff`` on EVERY ``/admin`` response.

    One cross-cutting header covering both the HTML/asset responses and the #35 JSON API
    responses (which serve UNTRUSTED ``suggested_title`` / ``origin`` verbatim) — a defense
    against a browser MIME-sniffing a JSON/text body into active HTML. Implemented as a
    pure-ASGI middleware so it adds ONLY a header and never touches the #35 JSON bodies or
    status codes. Frame defenses (CSP ``frame-ancestors`` + ``X-Frame-Options``) stay on the
    HTML/asset responses themselves, where framing is the threat.
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
                MutableHeaders(scope=message).setdefault(
                    "x-content-type-options", "nosniff"
                )
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


# --- HTML pages --------------------------------------------------------------
async def admin_page(request: Request) -> Response:
    """``GET /admin`` — the console page (auth-gated read; degraded-OK).

    Requires a valid cookie OR Bearer ADMIN_TOKEN via :func:`require_admin`; an
    unauthenticated hit raises 401 (acc 1). No ``require_operational`` — the console must
    render (reads) even in degraded mode (acc 8). The HTML itself holds no data: it fetches
    the JSON API on load.
    """
    await require_admin(request)
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
    """Read the token from a form POST (the login form) or a JSON body (curl/tests)."""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except Exception:
            return ""
        token = data.get("token") if isinstance(data, dict) else None
        return token if isinstance(token, str) else ""
    form = await request.form()
    token = form.get("token")
    return token if isinstance(token, str) else ""


async def login_submit(request: Request) -> Response:
    """``POST /admin/login`` — verify ADMIN_TOKEN → mint a cookie session (acc 2).

    The submitted token is compared CONSTANT-TIME against ``settings.admin_token``; on a
    match a random session id is minted (NEVER the token) and set as the hardened cookie,
    and the endpoint answers 200. A wrong/empty token is a flat 401. Not behind
    ``require_admin`` (this is how you AUTHENTICATE) and not behind ``require_operational``
    (you may log in to read while degraded); it touches only the in-memory store.
    """
    submitted = await _submitted_token(request)
    expected = request.app.state.settings.admin_token
    if not secrets.compare_digest(
        submitted.encode("utf-8", "ignore"), expected.encode("utf-8")
    ):
        auth_rejections.incr("admin_session")
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
