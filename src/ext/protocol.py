"""Wire protocol constants, small validators and the TabInfo->row mapper.

This is the canon of docs/architecture.md §6 "Протокол расширение ↔ сервис" for
the SERVICE side only: message ``type`` strings, reject-reason / error-code
strings, the mapping from a client ``TabInfo`` to a ``tabs`` row, and two pure
helpers (hello validation, the heartbeat miss decision). Nothing here does I/O or
touches the database — it is deliberately pure so the invariants can be unit
tested without a socket or a wall clock.
"""

from __future__ import annotations

from typing import Any

# --- Message types (the `type` field of every frame) ------------------------
TYPE_HELLO = "hello"
TYPE_HELLO_ACK = "hello_ack"
# Enrollment handshake (§2, issue #35). A not-yet-approved instance opens with an
# `enroll_request` (carrying the window `code`); the service records the request and
# answers `enroll_pending` (approval is async — no response is sent on approval, the
# client learns via a successful `hello` on its next alarm). A gated request is refused
# with `enroll_rejected{reason}`.
TYPE_ENROLL_REQUEST = "enroll_request"
TYPE_ENROLL_PENDING = "enroll_pending"
TYPE_ENROLL_REJECTED = "enroll_rejected"
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
# hello verdicts the client acts on (§7): the secret matched a REVOKED row, or matched
# no active/pending row at all (unknown — e.g. never approved, or deleted). The client
# distinguishes these to decide whether to re-enroll (unknown) or stop (revoked).
REJECT_REVOKED = "revoked"
REJECT_UNKNOWN = "unknown_instance"

# --- Enroll reject reasons (`reason` of an `enroll_rejected` frame) ----------
# The enrollment window is closed, the supplied window `code` was wrong/blank, or the
# pending-request list is at its ceiling. A protocolVersion mismatch reuses
# ``REJECT_PROTOCOL`` (same string as the hello path).
ENROLL_CLOSED = "closed"
ENROLL_BAD_CODE = "bad_code"
ENROLL_CAPACITY = "capacity"

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
    msg: dict[str, Any],
    protocol_version: int,
    resolved_instance_id: str | None,
    allowed_origins: set[str],
) -> str | None:
    """Validate a ``hello`` frame against config; return a reject reason or None.

    Secret-based (§2, issue #35): authentication is by the per-install SECRET, which
    the channel resolves to an active ``instances`` row (``secret_hash -> id``) BEFORE
    calling this. There is no shared token and no self-reported instanceId anymore — the
    id is server-assigned. So this pure helper only does the config-shaped checks that
    do not need the DB or the registry:

    Order mirrors §6: protocol version (exact int equality, never a silent downgrade)
    first; then ``resolved_instance_id`` — ``None`` means the secret matched no active
    instance, a last-line ``REJECT_AUTH`` guard (the channel normally rejects a
    revoked/unknown secret with a more specific reason BEFORE reaching here); then origin
    when an allow-list is configured. Duplicate-instance is NOT decided here — it needs
    the live registry — so it lives in the channel.
    """
    if msg.get("protocolVersion") != protocol_version:
        return REJECT_PROTOCOL
    # The secret already matched (or did not) in the channel via a hashed lookup — the
    # comparison is NOT done here (keeping this module pure and DB-free). A None id is
    # the belt-and-suspenders guard for "no active instance behind this secret".
    if resolved_instance_id is None:
        return REJECT_AUTH
    # Empty allow-list => accept ANY origin here, while /api/* CORS treats the same
    # empty list as CLOSED. Deliberate asymmetry — see parse_origins' docstring for the
    # full reasoning and for how the empty case is made audible (§12).
    if allowed_origins:
        origin = msg.get("origin")
        if origin not in allowed_origins:
            return REJECT_ORIGIN
    return None


def enroll_reject_reason(
    msg: dict[str, Any],
    protocol_version: int,
    window_open: bool,
    code_ok: bool,
    has_capacity: bool,
) -> str | None:
    """Decide whether an ``enroll_request`` is refused; return a reason or None.

    Pure so the enroll gate is unit-testable without a socket or the DB. **Order is
    load-bearing** (issue acceptance 2/3): protocol version first, then the WINDOW must
    be open, then the CODE must be correct, then there must be pending capacity — every
    one of these gates BEFORE the channel writes any ``enroll_requests`` row. In
    particular a request with a missing/blank code at an OPEN window must be refused
    (``bad_code``) and write NO row, so the channel passes ``code_ok=False`` for a
    missing/blank/mismatched code (acceptance 2).

    * protocolVersion mismatch -> ``REJECT_PROTOCOL`` (same string as the hello path)
    * not ``window_open``      -> ``ENROLL_CLOSED``
    * not ``code_ok``          -> ``ENROLL_BAD_CODE``
    * not ``has_capacity``     -> ``ENROLL_CAPACITY``
    * otherwise                -> ``None`` (accept, record the request)
    """
    if msg.get("protocolVersion") != protocol_version:
        return REJECT_PROTOCOL
    if not window_open:
        return ENROLL_CLOSED
    if not code_ok:
        return ENROLL_BAD_CODE
    if not has_capacity:
        return ENROLL_CAPACITY
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
