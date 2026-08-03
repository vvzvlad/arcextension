"""Request guards shared by every ``/api/*`` endpoint.

* :func:`require_api_caller` — the per-caller ``/api/*`` Bearer check (issue #35 §4).
  ADMIN_TOKEN authenticates the human/agent (``Caller("admin")``); any other Bearer is
  the RAW instance secret — the SAME credential the client sends on ``/ext`` hello —
  hashed server-side and resolved to an ACTIVE instance, yielding
  ``Caller("instance", id)``. Anything else is a flat 401; a DB failure during the
  instance lookup is a 503, never a silent pass (revocation must act instantly, so the
  resolution is NOT cached).
* :func:`require_metrics_token` — the SEPARATE Bearer ``METRICS_TOKEN`` check for
  ``/metrics`` only (§12: the scrape credential lives in git plaintext, so it must
  never be able to touch anything but ``/metrics``; it must NOT accept ``ADMIN_TOKEN``
  or a per-instance secret).
* :func:`require_operational` — 503 while the DB is in degraded mode (a migration
  failure means the schema cannot be trusted for authoritative writes). ``/healthz``
  deliberately does NOT call it: liveness must stay green so an orchestrator keeps
  routing to the container (§12).
* :func:`require_not_paused` — 423 while the emergency-stop pause is armed (§7), with
  the ``force=True`` exception reserved for the human's own buttons.
* :func:`read_force_body` — the optional-JSON-body reader those endpoints use to see
  ``{"force": true}`` before the gate runs.

Every 401 here increments the process-memory ``curator_auth_rejections_total``
counter (§12) via :mod:`src.api.auth_metrics` — a leaf module, so no import cycle.

Kept in their own module (no import of ``src.app``) so both the app factory and
the endpoint modules can import them without a cycle.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from starlette.exceptions import HTTPException
from starlette.requests import Request

from src.api.auth_metrics import auth_rejections

# Upper bound on a raw instance secret presented as an /api Bearer (mirrors the /ext
# _MAX_SECRET): a real secret is 64 hex chars, so 128 is generous; an oversized token is
# rejected before it can make the server hash a multi-MB string.
_MAX_INSTANCE_SECRET = 128

# Verbs that mutate state. A COOKIE-authenticated request using one of these must clear the
# same-origin CSRF gate (issue #36 acc 3); GET/HEAD reads and Bearer-authenticated requests
# do not (see :func:`require_same_origin`).
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _bearer_ok(request: Request, expected: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    # Constant-time compare so a wrong token cannot be probed byte-by-byte. Compare
    # as BYTES: compare_digest raises TypeError on a non-ASCII str (Starlette decodes
    # the header latin-1), which would turn a malformed header into a 500 instead of
    # a flat 401 — cheap DoS/log-noise, and it breaks a legit non-ASCII token.
    return scheme.lower() == "bearer" and secrets.compare_digest(
        token.encode("utf-8", "ignore"), expected.encode("utf-8")
    )


def _bearer_token(request: Request) -> str | None:
    """Return the raw Bearer token, or ``None`` when the header is missing/not Bearer."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


@dataclass(frozen=True)
class Caller:
    """Who authenticated an ``/api/*`` request (issue #35 §4).

    ``kind='admin'`` is the human at the startpage OR the MCP agent, both bearing
    ADMIN_TOKEN; ``instance_id`` is None. ``kind='instance'`` is a curated browser
    instance authenticating with its own RAW secret (the /ext hello credential), which
    the server hashes and matches; ``instance_id`` is the server-assigned id that secret
    resolved to.
    """

    kind: Literal["instance", "admin"]
    instance_id: str | None = None


def initiator_for(caller: Caller) -> str:
    """Map an ``/api/*`` caller to the ``actions.initiator`` it writes (issue #35 §5).

    An instance caller is the human at the startpage → ``'user'`` (§7's forced-button
    initiator). An admin caller is an ADMIN_TOKEN-authenticated ``/api/*`` write →
    ``'admin'`` (distinct from ``'mcp'``, the MCP transport, and ``'curator'``, the
    autonomous pass). Only the mutating force-verbs (restore, undo, merge_windows) use
    this; ``/api/focus`` still writes nothing.
    """
    return "user" if caller.kind == "instance" else "admin"


async def require_api_caller(request: Request) -> Caller:
    """Authenticate an ``/api/*`` request and return (and store) its :class:`Caller`.

    Order matters (issue #35 §4):

    1. A missing / non-Bearer header is a flat 401.
    2. **ADMIN first, no DB, constant-time**: a token equal to ADMIN_TOKEN is the
       admin caller — resolved before any DB touch so admin wins even against a token
       that might also happen to hash to an instance secret, and so admin auth never
       depends on the DB being reachable.
    3. Otherwise the token is the RAW instance secret (the same credential the client
       sent on /ext hello over TLS); the server hashes it and resolves it to an
       **active** instances row. Only the sha256 is stored, so a DB-only leak yields no
       usable credential. A match is the instance caller.
    4. A DB failure during that lookup is a **503, never a silent pass** — a revocation
       we cannot check must not fall through to admin/anon. No caching: revocation is
       instant, so the secret is resolved on every request.

    The result is stored on ``request.state.caller`` AND returned (callers may use
    either); the four force-verbs and the mutating verbs read ``.kind`` off it.
    """
    token = _bearer_token(request)
    if token is None:
        auth_rejections.incr("api_token")
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    # (2) ADMIN first — constant-time compare, no DB.
    if _bearer_ok(request, request.app.state.settings.admin_token):
        caller = Caller(kind="admin")
        request.state.caller = caller
        return caller

    # (3) Resolve the token as a RAW instance secret (server hashes it). Leaf import
    # (no cycle): src.db.queries never imports the api package. Cap the length first so a
    # hostile Authorization header cannot make the server hash a multi-MB string
    # (symmetric with the /ext hello + enroll caps); an oversized token matches nothing.
    if len(token) > _MAX_INSTANCE_SECRET:
        auth_rejections.incr("api_token")
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    from src.db.queries import resolve_secret

    try:
        resolved = await request.app.state.db.read(
            lambda c: resolve_secret(c, token)
        )
    except Exception:
        # (4) The revocation check itself failed. Fail CLOSED with 503 — never let a
        # DB outage degrade into an anonymous or admin pass.
        raise HTTPException(status_code=503, detail="auth backend unavailable")

    if resolved is not None and resolved[1] == "active":
        caller = Caller(kind="instance", instance_id=resolved[0])
        request.state.caller = caller
        return caller

    # No admin match and no active instance (unknown / revoked / pending secret) → 401.
    auth_rejections.incr("api_token")
    raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def require_same_origin(request: Request) -> None:
    """CSRF gate for COOKIE-authenticated MUTATING ``/admin`` requests (issue #36 acc 3).

    ``SameSite=Strict`` alone is NOT sufficient here: the service sits behind a SHARED
    Traefik on ``Host(curator.example.com)``, and any neighbor service on the same
    registrable domain (``neighbor.example.com``) is SAME-SITE to the browser — so its
    forged ``POST`` would still carry our cookie. We therefore demand a same-ORIGIN signal:

    * Prefer ``Sec-Fetch-Site`` — a browser-SET (thus unforgeable by page script) fetch
      metadata header. Only ``same-origin`` passes; ``same-site`` / ``cross-site`` /
      ``none`` are refused (``same-site`` is precisely the shared-Traefik neighbor threat).
    * Absent that header (an older client), fall back to matching the ``Origin`` header's
      host against the request ``Host``. Only an exact host match passes.

    A mutating cookie request with NEITHER a usable ``Sec-Fetch-Site`` nor a matching
    ``Origin`` is refused with **403**. Only the caller (``require_admin``) knows the auth
    was by cookie and the method mutates, so it invokes this; a GET or a Bearer request
    never reaches here.
    """
    if request.method.upper() not in _MUTATING_METHODS:
        return
    sec_fetch = request.headers.get("sec-fetch-site")
    if sec_fetch is not None:
        # Trust the browser's own classification when present. Exactly one value is same
        # origin; everything else (including the same-site neighbor) is refused.
        if sec_fetch == "same-origin":
            return
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin and host and urlsplit(origin).netloc == host:
        return
    raise HTTPException(status_code=403, detail="cross-origin request refused")


def require_metrics_token(request: Request) -> None:
    """Enforce ``Authorization: Bearer <METRICS_TOKEN>`` for ``/metrics``; 401 otherwise.

    A SEPARATE token (§12): the scrape credential lives in git plaintext, so it must
    open ``/metrics`` and nothing else. Compared ONLY against ``metrics_token``, so a
    valid ``ADMIN_TOKEN`` (or a per-instance secret) is rejected here.
    """
    if not _bearer_ok(request, request.app.state.settings.metrics_token):
        auth_rejections.incr("metrics_token")
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def require_operational(request: Request) -> None:
    """Raise 503 when the service is in degraded mode (see module docstring)."""
    if getattr(request.app.state, "degraded", False):
        raise HTTPException(status_code=503, detail="service degraded")


# Ceiling on a request body this service will BUFFER before any credential has been
# checked. 4 KiB is two orders of magnitude more than the only such body we accept (an
# ADMIN_TOKEN in a JSON object or a form field) and small enough that N concurrent
# unauthenticated posters cannot make the process the cheapest thing to attack: neither
# the app nor uvicorn imposes any limit of its own, so ``await request.json()`` on
# ``POST /admin/login`` would happily buffer a gigabyte from an anonymous peer.
MAX_UNAUTHENTICATED_BODY_BYTES = 4096


async def read_bounded_body(request: Request, limit: int) -> bytes:
    """Buffer at most ``limit`` bytes of the request body; **413** past that.

    Both halves matter. ``Content-Length`` is checked first so an honest oversized POST is
    refused without reading a byte, and the stream is then accumulated with a running
    check so a CHUNKED body (no ``Content-Length`` at all — the trivial way around a
    header check) is cut off at the same ceiling rather than buffered whole.

    The bytes are stashed in Starlette's own ``_body`` cache, which is exactly what
    ``Request.body()`` does, so a later ``request.json()`` / ``request.form()`` parses THIS
    bounded buffer instead of re-reading the (already consumed) stream. That cache is a
    PRIVATE attribute and there is no public equivalent in Starlette (``Request.body()``
    reads and writes the same ``self._body``, with no setter and no "already read this"
    hook), so the coupling is deliberate — and it is pinned by a test that reddens loudly
    if a Starlette upgrade stops honouring it, rather than letting the ceiling quietly
    stop applying.

    A refusal on a branch that leaves the WIRE readable is made sticky by pinning the cache
    to ``b""``, so no later reader can go back and buffer the body this call just refused.
    That is the declared-``Content-Length`` branch, which returns before reading a byte:
    without the pin a subsequent ``await request.body()`` read the whole oversized body and
    the ceiling bounded nothing. Nothing does that today (the 413 propagates out of the
    only caller), but "no caller happens to do it" is not a bound.

    The pin is deliberately NOT applied on the already-buffered branch. There the cache
    holds a body a previous reader legitimately read; wiping it destroys valid data to make
    a refusal stick that is already stuck — the stream is drained, so nothing can re-read
    the wire anyway. Sticky is a property of the two branches the wire can still be reached
    from, not of the 413.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            oversized = int(declared) > limit
        except ValueError:
            raise HTTPException(status_code=400, detail="malformed Content-Length")
        if oversized:
            # Nothing has been read yet: pin the cache empty so the refusal survives a
            # later reader (see the docstring).
            request._body = b""
            raise _too_large()
    if hasattr(request, "_body"):
        # Already buffered by an earlier reader; re-check rather than trust it. No pin
        # here — that buffer is somebody else's valid body, and there is no wire left to
        # re-read even if the caller swallows the 413.
        if len(request._body) > limit:
            raise _too_large()
        return request._body
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            # Cut off mid-stream: the remainder is unread and the consumed part is
            # unrecoverable, so pin the cache empty rather than leave a later reader to
            # either resume buffering the oversized body or trip over a consumed stream.
            request._body = b""
            raise _too_large()
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    return body


def _too_large() -> HTTPException:
    """The 413 itself. Making a refusal STICK is the caller's job — only two of the three
    branches have a wire left to protect (see :func:`read_bounded_body`)."""
    return HTTPException(status_code=413, detail="request body too large")


async def read_force_body(request: Request) -> dict:
    """Parse an OPTIONAL JSON-object request body; ``{}`` when there is none.

    Exists so the pause gate can see ``{"force": true}`` BEFORE it decides (§7). A
    present-but-malformed / non-object body is a flat 400 — the same shape the
    endpoints that already parse a body use, so nothing changes for them.
    """
    if not await request.body():
        return {}
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return data


async def require_not_paused(request: Request, *, force: bool = False) -> None:
    """Refuse a mutating ``/api/*`` verb while a pause is armed (§7).

    Mirrors :func:`require_operational` but for the pause "kill switch": a paused
    curator silences ALL automation, not just the pass, so every mutating verb answers
    ``paused {until}`` (§7 "Пауза глушит всю автоматику"). Applied to every mutating
    ``/api/*`` route EXCEPT the resume verbs (``POST``/``DELETE /api/pause`` — else an
    agent that paused by MCP could never lift it) and ``/api/run_pass`` (its own
    dry_run/confirm/pause logic lives in the runner). Reads ``pause_until`` from the DB
    — hence ``async`` — and raises **423 Locked** with a structured
    ``{"error": "paused", "until": <ms>}`` body (rendered by the app's dict-detail
    exception handler). 423 (the automation is locked) is used consistently for the
    pause gate; the MCP path returns the parallel ``ToolError("paused", …)``.

    ``force`` is §7's ONE exception: «Исключение — собственные кнопки человека, и то с
    явным ``force:true``, который пишется в ``actions`` как ``initiator=user``». The
    caller passes it ONLY for a verb that IS a button on the startpage, read from the
    request body via :func:`read_force_body`. Under issue #35 §4 the startpage human
    authenticates with the INSTANCE secret, so the caller ANDs ``force`` with
    ``caller.kind == "instance"``: an admin/agent bearing ADMIN_TOKEN never crosses the
    pause with ``{"force": true}`` (else it would bypass §7's kill-switch outside MCP).
    Those four force-verbs:

    * ``POST /api/focus`` — jump to a tab (§10),
    * ``POST /api/actions/:id/restore`` — bring a taken tab back (§10),
    * ``POST /api/passes/:id/undo`` — roll a pass back (§10),
    * ``POST /api/instances/:id/merge_windows`` — §9's «Кнопка "слить окна сейчас" на
      стартпейдже», kept explicitly as a human button «для случая, когда ждать час не
      хочется».

    It is deliberately NOT wired into rules CRUD, ``/api/rules/:id/reset``, quick-links
    or ``/api/exemptions`` (policy edits, not buttons), and NOT into any MCP tool: an
    agent is not a human at the keyboard, and a paused system exists precisely to stop
    the MCP caller (§7/§12).

    §7 adds that a forced action «пишется в `actions` как `initiator=user`». That applies
    to the verbs which MUTATE the world — restore, undo and merge_windows each write
    their row as ``initiator='user'`` and mark ``detail`` (``restore:force``,
    ``undo_close:force``, ``{"force": true}``) so a forced action is tellable apart in the
    archive without a new column. ``/api/focus`` writes NOTHING, on purpose: it activates
    a tab and raises a window, moving and closing nothing, while ``actions`` is the
    journal of what the curator DID to tabs. A row per jump would be pure noise in the
    archive the human reads to answer "why is this tab gone", and there is nothing to
    undo or reconcile. Forcing a jump through a pause is still gated and still explicit —
    it is simply not an archived event.
    """
    # Leaf import (no import cycle): pause.py never imports the api package.
    from src.curator.pause import read_pause_until

    until = await request.app.state.db.read(read_pause_until)
    if until is not None and until > int(time.time() * 1000):
        if force:
            return
        raise HTTPException(status_code=423, detail={"error": "paused", "until": until})
