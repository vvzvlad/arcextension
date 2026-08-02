"""Wire protocol constants, small validators and the TabInfo->row mapper.

This is the canon of docs/architecture.md §6 "Протокол расширение ↔ сервис" for
the SERVICE side only: message ``type`` strings, reject-reason / error-code
strings, the mapping from a client ``TabInfo`` to a ``tabs`` row, and two pure
helpers (hello validation, the heartbeat miss decision). Nothing here does I/O or
touches the database — it is deliberately pure so the invariants can be unit
tested without a socket or a wall clock.
"""

from __future__ import annotations

import hmac
from typing import Any

# --- Message types (the `type` field of every frame) ------------------------
TYPE_HELLO = "hello"
TYPE_HELLO_ACK = "hello_ack"
TYPE_SNAPSHOT_REQUEST = "snapshot_request"
TYPE_SNAPSHOT = "snapshot"
TYPE_PING = "ping"
TYPE_PONG = "pong"
# Service -> extension commands and their correlated replies (§6). A `command`
# carries an `id`; the matching `response` echoes that `id` and is either
# `{ok:true, result}` or `{ok:false, error:{code, message}}`.
TYPE_COMMAND = "command"
TYPE_RESPONSE = "response"

# --- Command verbs (§6 "Команды (сервис → расширение)") ---------------------
CMD_OPEN_TAB = "open_tab"
CMD_CLOSE_TAB = "close_tab"
CMD_GET_TAB = "get_tab"
CMD_FOCUS_TAB = "focus_tab"
CMD_NAVIGATE_TAB = "navigate_tab"
CMD_MERGE_WINDOWS = "merge_windows"
CMD_EXECUTE_JS = "execute_js"

# --- Command error codes (§6) -----------------------------------------------
# The `error.code` a failing `response` may carry. These are the extension-side
# codes; two service-side codes below name failures that never leave the service.
ERR_STALE_SESSION = "stale_session"
ERR_PRECONDITION_FAILED = "precondition_failed"
ERR_NO_SUCH_TAB = "no_such_tab"
ERR_NO_WINDOW = "no_window"
ERR_JS_DISABLED = "js_disabled"
ERR_BUSY_DRAGGING = "busy_dragging"
ERR_INTERNAL = "internal"
# Service-side only: no live socket for the instance, and the local send/wait
# timed out before any `response` arrived.
ERR_NO_CONNECTION = "no_connection"
ERR_TIMEOUT = "timeout"

# --- Reject reasons / hello_ack error codes ---------------------------------
# The `reject_reason` stored in `instances` and the `error.code` returned in a
# failing `hello_ack` use the SAME strings (§6).
REJECT_PROTOCOL = "protocol"
REJECT_AUTH = "auth"
REJECT_INSTANCE = "instance"
REJECT_DUPLICATE = "duplicate_instance"
REJECT_ORIGIN = "origin"

# Two consecutive heartbeat misses close the socket (§6).
MAX_HEARTBEAT_MISSES = 2


def heartbeat_step(alive: bool, misses: int) -> tuple[int, bool]:
    """Advance the heartbeat miss counter by one interval.

    Pure and clock-free so the disconnect rule is testable without timing.

    ``alive`` is the flag as observed at the tick: ``True`` means a ``pong`` was
    received since the previous ``ping`` was sent, ``False`` means none arrived.

    Returns ``(new_misses, should_disconnect)``. A received pong resets the
    counter; an interval with no pong increments it; ``MAX_HEARTBEAT_MISSES``
    consecutive misses (default 2) => disconnect.
    """
    new_misses = 0 if alive else misses + 1
    return new_misses, new_misses >= MAX_HEARTBEAT_MISSES


def parse_origins(raw: str) -> set[str]:
    """Parse the comma-separated ``EXT_ALLOWED_ORIGINS`` value into a set.

    Empty / blank => the EMPTY SET. **The two consumers of that empty set deliberately
    read it differently, and this is the one place both are written down:**

    * ``/ext`` (:func:`hello_reject_reason`, below) — **open**: an empty list skips the
      origin check entirely and any origin may connect.
    * ``/api/*`` CORS (:mod:`src.api.cors`) — **closed**: an empty list is an empty
      allow-list, so no ``Access-Control-Allow-Origin`` is emitted for anybody.

    The asymmetry is intentional and is the LESSER evil, not an oversight. The concrete
    ``chrome-extension://<id>`` is not knowable before the extension is loaded, so
    closing ``/ext`` by default would make the bootstrap impossible — and on an
    already-running deployment that never set the variable it would disconnect every
    instance at once, which is strictly worse than the CORS-closed state. CORS, by
    contrast, must never widen to ``*`` (§12), so its empty case can only be "closed".

    §12 warns that a MISMATCH between this list and the real extension id fails
    SILENTLY — ``/ext`` connects, the instance looks healthy, and only the startpage's
    ``fetch`` dies on preflight. Two things make the empty case audible instead:
    a one-time loud WARNING from each side at startup / first hello, and the
    ``curator_auth_rejections_total{reason="cors_preflight"}`` counter, which ticks on
    every preflight this list rejects (:class:`src.api.cors.CountingCORSMiddleware`).
    A NON-empty list that simply lists the wrong id is caught by the check below:
    ``reject_reason='origin'`` lands in ``instances`` and the status row goes red.
    """
    return {part.strip() for part in raw.split(",") if part.strip()}


def hello_reject_reason(
    msg: dict[str, Any], protocol_version: int, ext_token: str, allowed_origins: set[str]
) -> str | None:
    """Validate a ``hello`` frame against config; return a reject reason or None.

    Order mirrors §6: protocol version (exact int equality, never a silent
    downgrade), then token, then a non-blank instanceId (two instances sharing an
    id collide on ``PRIMARY KEY(instance_id, tab_id)`` and erase each other's
    tabs), then origin when an allow-list is configured. Duplicate-instance is
    NOT decided here — it needs the live registry — so it lives in the channel.
    """
    if msg.get("protocolVersion") != protocol_version:
        return REJECT_PROTOCOL
    # Constant-time compare — a short-circuiting `!=` leaks the token byte-by-byte
    # via timing. Compare as BYTES: compare_digest raises TypeError on a non-ASCII
    # str (Starlette/JSON can carry any codepoint), and this runs outside any
    # try/except, so a non-ASCII token would escape as an unhandled 500/crash.
    token = msg.get("token")
    token_bytes = token.encode("utf-8") if isinstance(token, str) else b""
    if not hmac.compare_digest(token_bytes, str(ext_token).encode("utf-8")):
        return REJECT_AUTH
    instance_id = msg.get("instanceId")
    if not isinstance(instance_id, str) or not instance_id.strip():
        return REJECT_INSTANCE
    # Empty allow-list => accept ANY origin here, while /api/* CORS treats the same
    # empty list as CLOSED. Deliberate asymmetry — see parse_origins' docstring for the
    # full reasoning and for how the empty case is made audible (§12).
    if allowed_origins:
        origin = msg.get("origin")
        if origin not in allowed_origins:
            return REJECT_ORIGIN
    return None


def _safe_int(value: Any) -> int:
    """Coerce a client-supplied age to int; a malformed value becomes 0 (fresh).

    A non-numeric ``ageMs``/``openedAgoMs`` must NOT raise and abort the whole
    snapshot transaction — that would drop an authenticated instance out of
    curation until it reconnects (same reasoning as the non-int tabId skip).
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def tab_info_to_row(instance_id: str, tab: dict[str, Any], now: int) -> tuple:
    """Map one client ``TabInfo`` to a positional ``tabs`` row tuple.

    Ages are converted to absolute server-clock timestamps and never allowed to
    exceed ``now`` (§5 "Возрасты вместо меток": a laptop clock that ran ahead must
    not produce a future timestamp): ``last_active_at = min(now, now - ageMs)``,
    ``opened_at = min(now, now - openedAgoMs)``. ``updated_at = now`` marks this
    write's server time — the `sent_at`-bounded delete relies on it.

    Column order matches ``_UPSERT_TAB`` below.
    """
    age_ms = _safe_int(tab.get("ageMs"))
    opened_ago_ms = _safe_int(tab.get("openedAgoMs"))
    last_active_at = min(now, now - age_ms)
    opened_at = min(now, now - opened_ago_ms)
    return (
        instance_id,
        tab.get("tabId"),
        tab.get("windowId"),
        tab.get("url"),
        tab.get("title"),
        tab.get("favIconUrl"),
        1 if tab.get("pinned") else 0,
        1 if tab.get("active") else 0,
        opened_at,
        last_active_at,
        1 if tab.get("ageUnknown") else 0,
        1 if tab.get("selfNavigating") else 0,
        1 if tab.get("audible") else 0,
        now,
    )
