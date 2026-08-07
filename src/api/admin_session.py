"""Admin SESSION cookie for the #36 HTML console — SIGNED and stateless.

The cookie a browser carries is NEVER the ADMIN_TOKEN (acc 2). It is
``<payload>.<signature>``: a base64url JSON payload carrying only an expiry, plus an
HMAC-SHA256 over it. Nothing is stored server-side.

WHY STATELESS, AFTER THIS WAS AN IN-MEMORY STORE. The store was per-process and
deliberately not persisted, on the reasoning that "a restart empties it and the operator
re-logs-in — acceptable for an admin console". In practice it is not: the service is
auto-updated by watchtower, so the container restarts on its own schedule and the owner
was re-typing the token at intervals he never chose. Verbatim: «почему эта хуйня
сбрасывается? он должен блядь быть вечным, я не хочу его вводить еще раз».

WHAT THE SIGNING KEY IS, AND WHY IT IS DERIVED FROM THE TOKEN. The key is
``sha256(b"curator-admin-session-v1|" + admin_token)``. That single choice preserves the
guarantee the old fingerprint field existed for (acc 4, ADMIN_TOKEN rotation kills every
session) without storing anything: a rotated token derives a different key, so every
cookie signed under the old one fails verification. It also means the key survives a
restart exactly as long as the token does, which is the whole point.

WHAT IS LOST, STATED PLAINLY. Server-side revoke. The old store could drop an id and end
that session for everyone holding it; a signed cookie cannot be un-signed. :func:`delete`
is therefore a CLIENT-side logout — the endpoint clears the browser's cookie — and a
copy someone else took stays valid until it expires. The real revoke is rotating
ADMIN_TOKEN, which invalidates every cookie at once. This is the honest price of not
being logged out by every deploy; it is not hidden behind a comment claiming otherwise.

Expiry is still checked SERVER-side: it lives inside the signed payload, so editing it in
the browser breaks the signature. The browser's own Max-Age is only a hint.

Kept a tiny leaf module (no import of ``src.app`` or the endpoint modules) so it is unit
testable against a bare ``SimpleNamespace`` app and imports without a cycle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

# The session cookie name. Its VALUE is a signed payload, never a secret.
COOKIE_NAME = "curator_admin_session"

# Key-derivation domain separator. Versioned: bumping this string is a one-line way to
# invalidate every outstanding cookie without touching ADMIN_TOKEN.
_KDF_CONTEXT = b"curator-admin-session-v1|"


def _now_ms() -> int:
    return int(time.time() * 1000)


def token_fingerprint(admin_token: str) -> str:
    """``sha256`` hex of the ADMIN_TOKEN. Retained because callers/tests import it; the
    cookie itself no longer carries a fingerprint — rotation is enforced by the signing
    key instead (see the module docstring)."""
    return hashlib.sha256(admin_token.encode("utf-8")).hexdigest()


def _key(admin_token: str) -> bytes:
    """The HMAC key for this ADMIN_TOKEN. Rotating the token rotates the key, which is
    what makes every previously issued cookie fail verification."""
    return hashlib.sha256(_KDF_CONTEXT + admin_token.encode("utf-8")).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def create(app, now: int | None = None) -> str:
    """Mint a signed cookie value valid for ``admin_session_ttl_min`` minutes.

    The payload holds ONLY the expiry — there is no session id to correlate and nothing
    to store. Two logins produce two independently valid cookies, which is the same
    behaviour the store had.
    """
    now = _now_ms() if now is None else now
    ttl_ms = app.state.settings.admin_session_ttl_min * 60_000
    payload = _b64(json.dumps({"exp": now + ttl_ms}, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_key(app.state.settings.admin_token), payload.encode("ascii"),
                   hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"


def validate(app, session_id: str | None, now: int | None = None) -> bool:
    """``True`` iff the cookie verifies under the CURRENT ADMIN_TOKEN and has not expired.

    Order matters: the signature is checked BEFORE the payload is trusted, so a forged or
    edited expiry never reaches the comparison. Any malformed value is simply False — a
    cookie is attacker-controlled input and must not be able to raise.
    """
    if not session_id or "." not in session_id:
        return False
    payload, _, sig = session_id.rpartition(".")
    try:
        expected = hmac.new(_key(app.state.settings.admin_token), payload.encode("ascii"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expected):
            return False
        claims = json.loads(_unb64(payload))
        exp = claims["exp"]
    except Exception:  # noqa: BLE001 - untrusted input: any parse failure is "invalid"
        return False
    if not isinstance(exp, int):
        return False
    return (_now_ms() if now is None else now) < exp


def delete(app, session_id: str | None) -> None:
    """Logout is CLIENT-side for a signed cookie: the endpoint clears the browser's copy,
    and there is nothing server-side to drop. Kept as a no-op so the endpoint's shape and
    its callers are unchanged — and so this docstring is where the limitation is stated
    rather than implied by a silently missing call. Rotate ADMIN_TOKEN to invalidate
    cookies that are already out."""
    return None
