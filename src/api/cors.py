"""CORS for ``/api/*`` — explicit ``chrome-extension://`` origins only, NEVER ``*``.

The startpage lives inside each extension and calls ``/api/*`` cross-origin; its
``Authorization: Bearer`` header forces a CORS preflight (§12), so the browser will
only issue the real request if this middleware echoes the exact requesting origin.
The allow-list is the SAME explicit set the ``/ext`` hello check uses
(:func:`~src.ext.protocol.parse_origins`) — kept in one place so a CORS/hello
mismatch (the §12 «бесшумный отказ») cannot arise from two divergent parsers.

Design invariants (do not relax):

* NEVER emit ``Access-Control-Allow-Origin: *`` — ``allow_origins`` is always an
  explicit list and ``allow_origin_regex`` is never set, so Starlette can only ever
  echo a listed origin or nothing.
* Empty list (dev, real extension id still unknown) => the list stays EMPTY, which
  BLOCKS all cross-origin ``/api/*`` (the secure default) — it is NOT widened to
  ``*``. A one-time loud warning is logged, mirroring the existing ``/ext`` open-
  origin warning, so prod is reminded to set ``EXT_ALLOWED_ORIGINS``.
* ``allow_credentials=False``: the token rides a Bearer header, not a cookie, so
  credentials stay off; with them off Starlette still echoes an explicit origin.
"""

from __future__ import annotations

from loguru import logger
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from src.api.auth_metrics import auth_rejections
from src.ext.protocol import parse_origins

# Headers the browser sends on a cross-origin /api/* call and must be allow-listed
# for the preflight to pass: Authorization (the Bearer token, forces the preflight),
# Content-Type (JSON bodies), Idempotency-Key (the quick-links offline flush, §10).
_ALLOW_HEADERS = ["authorization", "content-type", "idempotency-key"]
# The verbs actually registered under /api/* in src/app.py (GET/POST/PUT/DELETE) plus
# OPTIONS for the preflight itself. Deliberately NOT "*": keep it to the real surface.
_ALLOW_METHODS = ["GET", "POST", "OPTIONS", "PUT", "DELETE"]


class CountingCORSMiddleware(CORSMiddleware):
    """``CORSMiddleware`` that also counts REJECTED preflights (§12).

    §12 asks for a «счётчик отклонённых preflight» alongside the ``/ext``
    ``reject_reason='origin'`` signal: a preflight rejected here is the same silent-
    failure smell (a CORS list that no longer matches the real extension id), so it
    is folded into the process-memory ``curator_auth_rejections_total`` the origin
    reject already feeds — making the mismatch visible in Prometheus. Only the
    failure branch is touched; the happy path is Starlette's, unchanged.
    """

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        # Starlette returns 400 for a disallowed preflight (bad origin / method /
        # header) and 200 when it passes. Count only the rejections.
        if response.status_code != 200:
            auth_rejections.incr("cors_preflight")
        return response


def cors_kwargs(ext_allowed_origins: str) -> dict:
    """Build the ``CORSMiddleware`` kwargs from ``EXT_ALLOWED_ORIGINS``.

    ``allow_origins`` is the explicit, sorted allow-list parsed by the SAME
    :func:`~src.ext.protocol.parse_origins` the hello check uses — never ``*`` and
    never a widening ``allow_origin_regex``. An empty configured value yields an
    EMPTY list (cross-origin ``/api/*`` blocked, the secure default) and a one-time
    loud warning; it is never turned into ``*``.
    """
    origins = sorted(parse_origins(ext_allowed_origins))
    # The "never *" invariant is absolute: a literal "*" in the env would make
    # Starlette set allow_all_origins and echo ACAO: * (and would also break the hello
    # check, which compares origins literally). Drop it here so an operator typo can
    # never widen /api/* to any origin — the empty-list secure default applies instead.
    if "*" in origins:
        logger.warning(
            "EXT_ALLOWED_ORIGINS contains '*': dropped — /api/* CORS never allows any "
            "origin (§12). List explicit chrome-extension://<id> values instead."
        )
        origins = [o for o in origins if o != "*"]
    if not origins:
        logger.warning(
            "EXT_ALLOWED_ORIGINS is empty: cross-origin /api/* CORS is CLOSED "
            "(no Access-Control-Allow-Origin is emitted for any origin) — set "
            "EXT_ALLOWED_ORIGINS to the real chrome-extension://<id> before the "
            "startpage can fetch /api/* (§12)"
        )
    return {
        "allow_origins": origins,      # explicit list — Starlette never echoes '*'
        "allow_methods": _ALLOW_METHODS,
        "allow_headers": _ALLOW_HEADERS,
        "allow_credentials": False,    # Bearer header, not a cookie
    }
