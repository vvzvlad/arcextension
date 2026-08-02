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

    Empty / blank => empty set, which the caller treats as "accept any origin".
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
    # via timing. str-coerce both sides: compare_digest raises TypeError on a
    # non-str / str-vs-bytes mismatch, and this runs outside any try/except.
    if not hmac.compare_digest(str(msg.get("token") or ""), str(ext_token)):
        return REJECT_AUTH
    instance_id = msg.get("instanceId")
    if not isinstance(instance_id, str) or not instance_id.strip():
        return REJECT_INSTANCE
    # Empty allow-list => accept any origin (the concrete chrome-extension:// id
    # is unknown until the extension/generator phases; §12 tightens this later).
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
