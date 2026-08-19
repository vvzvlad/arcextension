"""Wire protocol constants, small validators and the TabInfo->row mapper.

This is the canon of docs/architecture.md §6 "Протокол расширение ↔ сервис" for
the SERVICE side only: message ``type`` strings, reject-reason / error-code
strings, the mapping from a client ``TabInfo`` to a ``tabs`` row, and two pure
helpers (hello validation, the heartbeat miss decision). Nothing here does I/O or
touches the database — it is deliberately pure so the invariants can be unit
tested without a socket or a wall clock.
"""

from __future__ import annotations

import re
from typing import Any

# --- Message types (the `type` field of every frame) ------------------------
TYPE_HELLO = "hello"
TYPE_HELLO_ACK = "hello_ack"
# Enrollment handshake (§6). A not-yet-enrolled instance opens with an `enroll_request`
# carrying the window `code` and the `instanceId` its operator typed; the OPEN WINDOW is
# the permission, so the service creates the ACTIVE row there and then and answers
# `enroll_accepted{instanceId}`. A gated request is refused with
# `enroll_rejected{reason}` and creates nothing.
#
# There is no `enroll_pending` anymore, and the frame is gone rather than kept as a
# no-op: it was the client's evidence that a request existed in a list awaiting a
# separate operator approval, and that list no longer exists. A client that still waits
# for it would wait forever instead of noticing it is already enrolled.
TYPE_ENROLL_REQUEST = "enroll_request"
TYPE_ENROLL_ACCEPTED = "enroll_accepted"
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
# Raise a window to the foreground by id, changing NOTHING inside it (no tab is
# activated). The startpage "click a space" jump uses this to bring a foreign
# instance's browser to the front without touching its tabs (§10/§14.1).
CMD_FOCUS_WINDOW = "focus_window"
CMD_NAVIGATE_TAB = "navigate_tab"
CMD_MERGE_WINDOWS = "merge_windows"
CMD_EXECUTE_JS = "execute_js"
# Move ONE tab to a window/position inside a SINGLE browser. Relocation BETWEEN
# instances is the open+close pair of §7, which works only because the browsers are
# separate processes; between the windows of one browser there was no verb at all.
CMD_MOVE_TAB = "move_tab"
# Read a page's text, and wait for a page condition. Both inject a FIXED function that is
# committed into the extension bundle and known at build time — NOT arbitrary code — so
# neither is behind the execute_js checkbox and neither writes a ``js_audit`` row. §12's
# argument for that gate is "усечённый код нереконструируем"; with a fixed function there
# is nothing to reconstruct. Every OTHER gate still applies to them, exactly as to every
# command: the pause/stop gate, the revoke check, ``stale_session``, and the extension's
# own http/https edge guard on the target tab.
CMD_GET_TEXT = "get_text"
CMD_WAIT_FOR = "wait_for"
# Set the value of a controlled (React/Vue) input in ONE call. The extension writes through
# the element's NATIVE prototype value setter (a plain ``el.value = …`` is reverted by React's
# value-tracker on the next render) and then dispatches bubbling ``input`` / ``change`` events,
# with ``contenteditable`` coverage. FIXED-function like ``get_text`` / ``wait_for``: ``selector``
# and ``value`` are DATA fed to ``querySelector`` and a value assignment, never source spliced
# into a page eval — so it is NOT behind the execute_js checkbox and writes NO ``js_audit`` row.
# It IS a MUTATION (it writes into the page as the user), so the service side gates it behind the
# pause/stop switch, exactly like ``navigate_tab`` — as it gates every verb that reaches the
# browser, the read-only fixed ones (``get_text`` / ``wait_for``) included; the mutation is what
# sets set_input apart from those, not the gate.
CMD_SET_INPUT = "set_input"
# Scroll a tab until the element count for a selector stops growing (or a target / deadline
# is hit). FIXED-function like ``get_text`` / ``wait_for``: its parameters are selectors and
# a direction — DATA fed to ``querySelector`` / ``scrollTop``, never source spliced into a
# page eval — so it is NOT behind the execute_js checkbox and writes NO ``js_audit`` row.
# The scheduling loop lives in the extension's service worker (a fresh ``chrome.scripting``
# inject per step), so its pacing survives a page throttling its own timers.
CMD_SCROLL_UNTIL = "scroll_until"
# The Job-API pair, formalising "fire arbitrary code into a page global, then poll it":
#   * ``start_js`` carries ARBITRARY caller code, so it is behind the execute_js checkbox +
#     a ``js_audit`` row, EXACTLY like ``execute_js`` (§12 —
#     :func:`src.ext.commands.send_command` audits both before the send). It wraps the code
#     in an async IIFE that stashes ``{state, value|message}`` under
#     ``window.__curatorJobs[jobId]`` and returns ``{jobId}`` WITHOUT awaiting the promise.
#   * ``poll_job`` is a FIXED read of that global (the injected body takes only the jobId as
#     DATA), so — like ``get_text`` — no checkbox and no audit row. Job state lives IN THE
#     PAGE and dies with the tab (reload/discard/close): ergonomics, not durability.
CMD_START_JS = "start_js"
CMD_POLL_JOB = "poll_job"
# The first verb down the chrome.debugger (CDP) path (§12, wave 18). Toggles
# ``Emulation.setFocusEmulationEnabled`` so a BACKGROUND tab behaves as focused (no timer
# throttling) without taking the screen from the human. STATEFUL: the emulation holds only
# while the debugger stays attached, so enable = attach + command + KEEP attached, and
# disable = command(false) + detach. Gated by the SINGLE JS & Debugger checkbox at the
# extension edge, but carries NO arbitrary code and writes NO ``js_audit`` row — it only
# fakes focus, exfiltrating nothing; a later data-bearing CDP verb needs its own audit.
CMD_SET_FOCUS_EMULATION = "set_focus_emulation"
# WebSocket-frame capture (§12, wave 21) — the FIRST data-bearing verb down the
# chrome.debugger path. ``start_ws_capture`` attaches the debugger and turns on ``Network.*``
# event delivery so the extension buffers a tab's WS frames; ``read_ws_frames`` drains that
# buffer to the agent; ``stop_ws_capture`` disables the domain and detaches. Because the frames
# ship PAGE DATA out (a messenger's conversation — phones, sums, addresses), ``start_ws_capture``
# is behind the SAME single JS & Debugger checkbox as ``execute_js`` AND writes a ``js_audit``
# row before the send (:func:`src.ext.commands.send_command` — it is on the audit ALLOWLIST,
# unlike ``set_focus_emulation`` which fakes focus and exfiltrates nothing). ``read_ws_frames`` is
# a FIXED, draining read of the already-authorised buffer (no second audit) but is size-capped
# for privacy; ``stop_ws_capture`` is pure idempotent teardown. One debugger client per tab, so a
# tab held by focus emulation OR an active capture refuses a second start (``debugger_attach``).
CMD_START_WS_CAPTURE = "start_ws_capture"
CMD_READ_WS_FRAMES = "read_ws_frames"
CMD_STOP_WS_CAPTURE = "stop_ws_capture"
# Wake a DISCARDED tab (issue #68). The browser unloads an idle tab from memory to save RAM;
# the tab still EXISTS (``chrome.tabs.get`` answers it, with ``discarded:true`` and its url) but
# every injecting/attaching verb fails on it with the opaque "Extension manifest must request
# permission…". ``wake_tab`` reloads it — a reload re-materialises a discarded tab — and waits for
# ``status:complete``. A FIXED action, not arbitrary code (like ``navigate_tab``): no execute_js
# checkbox, no ``js_audit`` row. It IS a mutation (it reloads), so the SERVICE side gates it behind
# the pause/stop switch. Answers ``{wasDiscarded}`` so the caller learns whether it was a real wake
# or a reload of an already-live tab — ``wake_tab`` ALWAYS reloads, so on a live tab it is a full
# reload that loses page state, not a no-op.
CMD_WAKE_TAB = "wake_tab"
# Capture a tab's pixels — an ORDINARY read verb, the picture twin of ``get_text`` (get_text reads
# the same page as text, uncropped and unaudited). HYBRID by the target's focus: an ACTIVE tab is
# snapped cheaply with ``chrome.tabs.captureVisibleTab`` (no debugger, no ``js_audit`` row, no OS
# focus taken); a BACKGROUND tab needs the chrome.debugger ``Page.captureScreenshot`` path (a
# TRANSIENT attach — capture — detach, never held), which rides the SAME single JS & Debugger
# checkbox as ``execute_js``. ``selector`` is OPTIONAL: absent, the whole visible frame; present, a
# crop to the element's bounding box (cheap path via ``OffscreenCanvas``, debugger path via a native
# CDP clip). A one-shot READ like ``get_text``, so it is NOT on the audit allowlist and writes NO
# ``js_audit`` row. The SERVICE side gates it behind the pause switch (it reaches the browser) and
# caps the returned image's size, refusing an oversized frame with ``precondition_failed``.
CMD_SCREENSHOT = "screenshot"

# --- Command error codes (§6) -----------------------------------------------
# The `error.code` a failing `response` may carry. These are the extension-side
# codes; two service-side codes below name failures that never leave the service.
ERR_STALE_SESSION = "stale_session"
ERR_PRECONDITION_FAILED = "precondition_failed"
ERR_NO_SUCH_TAB = "no_such_tab"
# The target tab EXISTS but the browser has DISCARDED it — unloaded it from memory to save RAM
# (issue #68). It still answers ``chrome.tabs.get`` (``discarded:true`` + its url), so it is not
# ``no_such_tab``; its url is usually http/https, so it is not the ``precondition_failed`` "wrong
# scheme" either. But every injecting/attaching verb fails on it with the opaque "Extension manifest
# must request permission…", which blames the manifest for a tab that only needs waking. Its OWN code
# so the agent calls ``wake_tab`` (or ``navigate_tab`` to its url) instead of chasing a permission bug.
ERR_TAB_DISCARDED = "tab_discarded"
ERR_NO_WINDOW = "no_window"
ERR_JS_DISABLED = "js_disabled"
ERR_BUSY_DRAGGING = "busy_dragging"
# ``move_tab`` refused: the tab is PINNED and the move would cross a window boundary
# (§9 — a cross-window ``tabs.move`` silently drops ``pinned``). Its own code so the
# caller can tell the owner's "do not touch by hand" shield apart from every other
# precondition without parsing a message.
ERR_PINNED_CROSS_WINDOW = "pinned_cross_window"
# ``set_focus_emulation`` could not attach the debugger to the target tab (§12): DevTools is
# open on it, or another debugger client already holds it (a tab takes ONE debugger client).
# Its own code so the agent can tell "unattachable tab" from every other precondition.
ERR_DEBUGGER_ATTACH = "debugger_attach"
ERR_INTERNAL = "internal"
# Service-side only: no live socket for the instance, and the local send/wait
# timed out before any `response` arrived.
ERR_NO_CONNECTION = "no_connection"
# Service-side only, and it keeps EXACTLY ONE producer even now that §11 has waiting
# verbs: no ``response`` frame arrived inside the command budget, so the browser's state is
# UNKNOWN — the command may have run, may not have, and the agent must NOT blindly retry.
# A ``wait_for`` whose page condition never came true is the opposite fact (the browser
# answered; the verdict is simply "no") and is therefore returned as ``ok:true`` with
# ``matched:false``. Two different facts, two different response SHAPES — which is what
# survives the wire, where a shared error string would not.
ERR_TIMEOUT = "timeout"

# --- Reject reasons / hello_ack error codes ---------------------------------
# The `reject_reason` stored in `instances` and the `error.code` returned in a
# failing `hello_ack` use the SAME strings (§6).
REJECT_PROTOCOL = "protocol"
REJECT_AUTH = "auth"
REJECT_INSTANCE = "instance"
REJECT_DUPLICATE = "duplicate_instance"
# There is no `origin` verdict anymore, and no `origin` anywhere. The check was
# self-reported by the very client it was meant to vet, and the EXT_ALLOWED_ORIGINS
# allow-list behind it is gone (see src/api/cors.py). The hello frame still CARRIES an
# origin and the service ignores it entirely — neither compared nor stored. The ENROLL
# path used to RECORD its frame's origin, as operator-facing evidence at approval time;
# with approval gone there is no reader for it, so the enroll_request no longer carries
# one either (a field nobody reads is how a contract drifts).
# hello verdicts the client acts on (§7): the secret matched a REVOKED row, or matched
# no active/pending row at all (unknown — e.g. never approved, or deleted). The client
# distinguishes these to decide whether to re-enroll (unknown) or stop (revoked).
REJECT_REVOKED = "revoked"
REJECT_UNKNOWN = "unknown_instance"

# --- Enroll reject reasons (`reason` of an `enroll_rejected` frame) ----------
# The enrollment window is closed, or the supplied window `code` was wrong/blank. A
# protocolVersion mismatch reuses ``REJECT_PROTOCOL`` (same string as the hello path).
ENROLL_CLOSED = "closed"
ENROLL_BAD_CODE = "bad_code"
# The proposed `instanceId` is already carried by an ACTIVE instance. This is the ONE
# objection the removed approval step really answered — a name collision — and it is
# refused loudly rather than turned into a manual step on every ordinary enrolment. An
# EXISTING-BUT-REVOKED id is NOT this: taking it back is the restore path for a revoked
# MAIN and is accepted (see ``queries.enroll_instance``).
ENROLL_ID_TAKEN = "id_taken"
# The proposed `instanceId` is outside the charset/length the row's PRIMARY KEY accepts
# (:data:`INSTANCE_ID_RE`). The extension validates the same expression before it sends,
# so reaching this means a client that skipped its own field check.
ENROLL_BAD_ID = "bad_id"

# The charset/length an instance id must satisfy — it becomes the ``instances`` PRIMARY
# KEY and travels into URLs, metric labels and the console, so a pasted or fat-fingered
# value must not become a permanent key. Lives HERE, in the pure module, because the
# enrollment gate that applies it is now the /ext channel; it used to live in
# src/api/admin.py next to the operator-assigned id of the retired approve endpoint.
INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def instance_id_ok(value: Any) -> bool:
    """Whether ``value`` is a usable instance id (a str matching :data:`INSTANCE_ID_RE`)."""
    return isinstance(value, str) and bool(INSTANCE_ID_RE.match(value))

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


def hello_reject_reason(
    msg: dict[str, Any],
    protocol_version: int,
    resolved_instance_id: str | None,
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
    revoked/unknown secret with a more specific reason BEFORE reaching here).
    Duplicate-instance is NOT decided here — it needs the live registry — so it lives in
    the channel.

    There used to be a third check: ``origin`` against the ``EXT_ALLOWED_ORIGINS``
    allow-list. It is gone. The value it compared is SELF-REPORTED by the peer being
    vetted, so it never held against anything that was not already authenticated by the
    secret; the allow-list's real consumer was ``/api/*`` CORS, and that was retired too
    (:mod:`src.api.cors` carries the argument).
    """
    if msg.get("protocolVersion") != protocol_version:
        return REJECT_PROTOCOL
    # The secret already matched (or did not) in the channel via a hashed lookup — the
    # comparison is NOT done here (keeping this module pure and DB-free). A None id is
    # the belt-and-suspenders guard for "no active instance behind this secret".
    if resolved_instance_id is None:
        return REJECT_AUTH
    return None


def enroll_reject_reason(
    msg: dict[str, Any],
    protocol_version: int,
    window_open: bool,
    code_ok: bool,
) -> str | None:
    """Decide whether an ``enroll_request`` is refused by the CONFIG-shaped gates.

    Pure so the enroll gate is unit-testable without a socket or the DB. **Order is
    load-bearing** (issue acceptance 2/3): protocol version first — the one check that
    needs neither the DB nor the window — then the WINDOW must be open, then the CODE must
    be correct. In particular a request with a missing/blank code at an OPEN window must be
    refused (``bad_code``) and write NO row, so the channel passes ``code_ok=False`` for a
    missing/blank/mismatched code (acceptance 2).

    * protocolVersion mismatch -> ``REJECT_PROTOCOL`` (same string as the hello path)
    * not ``window_open``      -> ``ENROLL_CLOSED``
    * not ``code_ok``          -> ``ENROLL_BAD_CODE``
    * otherwise                -> ``None`` (this helper accepts; see below)

    ``None`` is NOT "the request is accepted" — it is "the config gates passed". Three
    more gates run afterwards in :mod:`src.ext.channel`, and they are not here because
    they cannot be pure:

    1. the STRUCTURAL check (non-blank, non-oversized ``installUuid``/``secret``), done
       after the code gate so a wrong code never reveals whether the frame was well-formed;
    2. the id CHARSET check (:func:`instance_id_ok`) → ``ENROLL_BAD_ID``, which is pure but
       lives with the structural checks so the whole "is this frame usable" step is in one
       place and refuses before any write;
    3. the id COLLISION, decided INSIDE the write transaction by
       :func:`src.db.queries.enroll_instance` — ``ENROLL_ID_TAKEN`` is returned by the
       channel from that transaction's outcome, because a pre-read in its own transaction
       could never be authoritative against two browsers enrolling the same name at once.

    This function used to take a ``has_capacity`` flag and own ``ENROLL_CAPACITY``, and its
    docstring claimed every gate ran "BEFORE the channel writes any ``enroll_requests``
    row". That stopped being true when the capacity decision moved into the write (a
    pre-read in its own transaction could never be authoritative against racing enrolls):
    the only caller passed ``has_capacity=True`` unconditionally, so the branch was dead
    code and the documented order was the opposite of the real one. The parameter is gone
    rather than left as a trap for the next reader — and with the pending list itself, so
    is the capacity ceiling.
    """
    if msg.get("protocolVersion") != protocol_version:
        return REJECT_PROTOCOL
    if not window_open:
        return ENROLL_CLOSED
    if not code_ok:
        return ENROLL_BAD_CODE
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
