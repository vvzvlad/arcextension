"""In-memory admin SESSION store for the #36 HTML console.

The cookie a browser carries is a RANDOM opaque id — NEVER the ADMIN_TOKEN (acc 2). That
id maps, in process memory on ``app.state.admin_sessions``, to
``(expires_at_ms, admin_token_fingerprint)`` where the fingerprint is
``sha256(settings.admin_token)``. Keeping the mapping server-side (rather than trusting a
signed/Max-Age cookie) buys three guarantees a bare cookie cannot:

* **Expiry is checked server-side** — a cookie replayed past its stored ``expires_at_ms``
  is invalid regardless of the browser's ``Max-Age`` hint, so a leaked/stale id cannot be
  resurrected by editing the client-side cookie.
* **Revoke** — :func:`delete` drops the id, so logout truly ends the session on the server.
* **ADMIN_TOKEN rotation kills every session** (acc 4) — :func:`validate` compares the
  session's stored fingerprint against the CURRENT ``sha256(settings.admin_token)``; a
  changed token no longer matches, so every session minted under the old token is dead.

The store is per-process and deliberately NOT persisted: a restart empties it and the
operator re-logs-in — acceptable for an admin console (§13). Expired ids are pruned
opportunistically on each :func:`validate` (drop-on-touch), so no timer/sweeper is needed.

Kept a tiny leaf module (no import of ``src.app`` or the endpoint modules) so it is unit
testable against a bare ``SimpleNamespace`` app and imports without a cycle.
"""

from __future__ import annotations

import hashlib
import secrets
import time

# The session cookie name. Its VALUE is always the random id below, never a secret.
COOKIE_NAME = "curator_admin_session"


def _now_ms() -> int:
    return int(time.time() * 1000)


def token_fingerprint(admin_token: str) -> str:
    """``sha256`` hex of the current ADMIN_TOKEN — the per-session witness that lets a
    rotated token invalidate every existing session (acc 4). It is a one-way hash, so
    storing it leaks nothing that would recover the token."""
    return hashlib.sha256(admin_token.encode("utf-8")).hexdigest()


def _store(app) -> dict:
    """The per-process ``session_id -> (expires_at_ms, fingerprint)`` map.

    Created lazily so a test (or the app lifespan) that never logs in still has a valid,
    empty store — validation of any id then simply returns ``False``.
    """
    store = getattr(app.state, "admin_sessions", None)
    if store is None:
        store = {}
        app.state.admin_sessions = store
    return store


def create(app, now: int | None = None) -> str:
    """Mint a random ``session_id``, store ``(now + TTL, fingerprint)``, return the id.

    The id is ``secrets.token_urlsafe(32)`` — cryptographically random, ~256 bits, and
    completely decoupled from the ADMIN_TOKEN. TTL comes from ``admin_session_ttl_min``.
    """
    now = _now_ms() if now is None else now
    ttl_ms = app.state.settings.admin_session_ttl_min * 60_000
    fingerprint = token_fingerprint(app.state.settings.admin_token)
    store = _store(app)
    # Bounded store: drop-on-touch (in validate) never reaches an id that is issued and then
    # never re-presented (e.g. a closed browser tab), so those would linger forever. A full
    # sweep of expired entries on each login — a rare, human-paced event — keeps the store
    # bounded by the count of LIVE sessions, not of every session ever minted.
    expired = [sid for sid, (exp, _fp) in store.items() if now >= exp]
    for sid in expired:
        del store[sid]
    session_id = secrets.token_urlsafe(32)
    store[session_id] = (now + ttl_ms, fingerprint)
    return session_id


def validate(app, session_id: str | None, now: int | None = None) -> bool:
    """``True`` iff ``session_id`` is a live session minted under the CURRENT ADMIN_TOKEN.

    A ``None``/empty id (no cookie) is ``False`` without touching the store. An entry that
    is past its ``expires_at_ms`` OR whose stored fingerprint no longer matches the current
    ``sha256(admin_token)`` is both rejected AND evicted (drop-on-touch), so an expired or
    post-rotation id cannot be replayed.
    """
    if not session_id:
        return False
    now = _now_ms() if now is None else now
    store = _store(app)
    entry = store.get(session_id)
    if entry is None:
        return False
    expires_at, fingerprint = entry
    current = token_fingerprint(app.state.settings.admin_token)
    # Constant-time fingerprint compare (both are fixed-length hex); the TTL check is a
    # plain integer comparison. Either failure evicts the id and refuses it.
    if now >= expires_at or not secrets.compare_digest(fingerprint, current):
        store.pop(session_id, None)
        return False
    return True


def delete(app, session_id: str | None) -> None:
    """Drop a session id (logout). Idempotent — a missing/None id is a no-op."""
    if session_id:
        _store(app).pop(session_id, None)
