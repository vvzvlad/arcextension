"""CORS for ``/api/*`` — ANY origin is allowed, deliberately.

The startpage lives inside each extension and calls ``/api/*`` cross-origin; its
``Authorization: Bearer`` header forces a CORS preflight (§12), so the browser only
issues the real request if this middleware answers that preflight.

**The old "NEVER emit ``Access-Control-Allow-Origin: *``" invariant is GONE. It was
removed on purpose — do not restore it as "obviously needed".** It used to be an
allow-list of ``chrome-extension://<id>`` origins parsed from ``EXT_ALLOWED_ORIGINS``,
and re-reading what it actually bought:

* every ``/api/*`` route is already behind ``require_api_caller`` — an ``ADMIN_TOKEN`` or
  an enrolled instance's secret. CORS was never the lock on that door, only a second one;
* ``allow_credentials=False`` — nothing ambient (cookie, TLS client cert, HTTP auth) is
  ever sent cross-origin, so there is no ambient session for a foreign page to ride and
  no classic CSRF to prevent. That is exactly the condition under which ``*`` is the
  standard, safe answer;
* CORS is enforced by BROWSERS ONLY. A script, curl or bot ignores it completely, so the
  list never stopped an attacker — only a page, and only a page that had no credential;
* the only unauthenticated readable route is ``/healthz``, and the service is required to
  be unreachable from the public internet (``deploy/DEPLOY.md`` §3).

The price was not free: a signing key that pinned the extension id and could never be
lost, plus an edit to the server's environment for every new machine. A second lock on a
door whose first lock is a token, inside a network nobody outside can reach, did not pay
for that.

Remaining invariant (this one does not relax): ``allow_credentials=False``. It is what
makes ``*`` safe, and Starlette refuses to combine credentials with a wildcard anyway.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from src.api.auth_metrics import auth_rejections

# Headers the browser sends on a cross-origin /api/* call and must be allow-listed
# for the preflight to pass: Authorization (the Bearer token, forces the preflight),
# Content-Type (JSON bodies), Idempotency-Key (the quick-links offline flush, §10).
_ALLOW_HEADERS = ["authorization", "content-type", "idempotency-key"]
# The verbs actually registered under /api/* in src/app.py (GET/POST/PUT/DELETE) plus
# OPTIONS for the preflight itself. Deliberately NOT "*": keep it to the real surface.
_ALLOW_METHODS = ["GET", "POST", "OPTIONS", "PUT", "DELETE"]
# Methods and headers stay EXPLICIT even though origins no longer are, and the reason is
# different from the one origins had. They are not a security boundary either — they are
# the declared shape of /api/*, and a mismatch between what the startpage sends and what
# is listed here is a code bug that fails in the §12 «бесшумный отказ» shape: the socket
# stays connected, the instance looks healthy, and only the fetch dies at the preflight.
# Keeping them narrow is what leaves that bug something to trip over (see below).


class CountingCORSMiddleware(CORSMiddleware):
    """``CORSMiddleware`` that also counts REJECTED preflights (§12).

    Still reachable after origins were opened up, on a NARROWER cause. Starlette rejects
    a preflight on three grounds — origin, method, header — and only the origin ground is
    gone: ``allow_origins=["*"]`` accepts every origin, so what remains is a preflight
    whose ``Access-Control-Request-Method`` is outside ``_ALLOW_METHODS`` or whose
    ``Access-Control-Request-Headers`` names something outside ``_ALLOW_HEADERS``.

    That is no longer a deploy-time misconfiguration (there is no env var to get wrong
    anymore) but a CODE bug: a new verb or a new custom header added to the startpage's
    fetch without being added here. It fails exactly as silently as the old origin
    mismatch did — websocket up, instance green, newtab rendering a cache that never
    refreshes — which is why the counter is kept rather than deleted along with the
    allow-list. Only the failure branch is touched; the happy path is Starlette's.
    """

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        # Starlette returns 400 for a disallowed preflight (bad method / header) and 200
        # when it passes. Count only the rejections.
        if response.status_code != 200:
            auth_rejections.incr("cors_preflight")
        return response


def cors_kwargs() -> dict:
    """Build the ``CORSMiddleware`` kwargs: any origin, credentials off.

    Takes no configuration on purpose — there is no allow-list to build and no env var
    behind it anymore (see the module docstring for why). ``allow_origin_regex`` is still
    never set: with ``allow_origins=["*"]`` Starlette sets ``allow_all_origins`` and emits
    a literal ``Access-Control-Allow-Origin: *``, which is well-defined only because
    credentials are off.
    """
    return {
        "allow_origins": ["*"],        # any origin; the lock on /api/* is the token
        "allow_methods": _ALLOW_METHODS,
        "allow_headers": _ALLOW_HEADERS,
        "allow_credentials": False,    # Bearer header, not a cookie — what makes '*' safe
    }
