"""Request guards shared by every ``/api/*`` endpoint.

* :func:`require_ext_token` — the Bearer ``EXT_TOKEN`` check (§12: one token opens
  ``/ext``, ``/api/*`` and ``/mcp``). A missing/wrong token is a flat 401.
* :func:`require_operational` — 503 while the DB is in degraded mode (a migration
  failure means the schema cannot be trusted for authoritative writes). ``/healthz``
  deliberately does NOT call it: liveness must stay green so an orchestrator keeps
  routing to the container (§12).

Kept in their own module (no import of ``src.app``) so both the app factory and
the endpoint modules can import them without a cycle.
"""

from __future__ import annotations

import secrets

from starlette.exceptions import HTTPException
from starlette.requests import Request


def require_ext_token(request: Request) -> None:
    """Enforce ``Authorization: Bearer <EXT_TOKEN>``; raise 401 otherwise."""
    expected = request.app.state.settings.ext_token
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    # Constant-time compare so a wrong token cannot be probed byte-by-byte. Compare
    # as BYTES: compare_digest raises TypeError on a non-ASCII str (Starlette decodes
    # the header latin-1), which would turn a malformed header into a 500 instead of
    # a flat 401 — cheap DoS/log-noise, and it breaks a legit non-ASCII EXT_TOKEN.
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.encode("utf-8", "ignore"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def require_operational(request: Request) -> None:
    """Raise 503 when the service is in degraded mode (see module docstring)."""
    if getattr(request.app.state, "degraded", False):
        raise HTTPException(status_code=503, detail="service degraded")
