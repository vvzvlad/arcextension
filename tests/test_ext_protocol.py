"""Pure-unit tests for the /ext protocol decision helpers (no socket, no DB).

``hello_reject_reason`` and ``enroll_reject_reason`` are deliberately pure so the reject
ORDER — which is load-bearing for enrollment acceptance 2/3 — can be pinned here without
a websocket. Each branch and the ordering between branches is asserted.

This file also owns the **mirror test** between this module's ``CMD_*`` / ``ERR_*``
strings and the extension's ``extension/src/constants.js``. Both files carry a comment
saying the two sides MUST agree, and a comment is the one thing that cannot enforce it:
the halves live in different languages, ship in different artifacts and are only ever
compared at runtime on a socket, where a drift shows up as a verb the extension answers
with ``internal: unknown command`` — in production, on the owner's browser.
"""

import re
from pathlib import Path

from src.ext import protocol
from src.ext.protocol import (
    ENROLL_BAD_CODE,
    ENROLL_CLOSED,
    REJECT_AUTH,
    REJECT_PROTOCOL,
    enroll_reject_reason,
    hello_reject_reason,
    instance_id_ok,
)


# --- hello_reject_reason (secret-based) --------------------------------------
def test_hello_protocol_mismatch_wins_first():
    # Even with a resolved id, a wrong protocolVersion rejects first.
    msg = {"protocolVersion": 2, "origin": "chrome-extension://x"}
    assert hello_reject_reason(msg, 1, "i1") == REJECT_PROTOCOL


def test_hello_none_resolved_id_is_auth():
    # The last-line guard: no active instance behind the secret => auth.
    msg = {"protocolVersion": 1}
    assert hello_reject_reason(msg, 1, None) == REJECT_AUTH


def test_hello_any_origin_is_accepted():
    # The origin check is GONE (there is no allow-list to check against, and the value was
    # self-reported by the peer being vetted anyway). Redden: re-add an origin verdict and
    # one of these starts rejecting.
    for origin in ("chrome-extension://anything", "https://not-an-extension.example", None):
        msg = {"protocolVersion": 1, "origin": origin}
        assert hello_reject_reason(msg, 1, "i1") is None, origin
    assert not hasattr(protocol, "REJECT_ORIGIN")
    assert not hasattr(protocol, "parse_origins")


def test_hello_no_token_field_consulted():
    # There is no longer any token in the decision — a hello WITHOUT a token still passes.
    msg = {"protocolVersion": 1}
    assert hello_reject_reason(msg, 1, "i1") is None


# --- enroll_reject_reason: each branch + the load-bearing order --------------
# The helper owns the CONFIG-shaped gates ONLY (protocol -> window -> code). Capacity used
# to be a fifth parameter here; it moved into the write transaction, after which the only
# caller pinned `has_capacity=True` and the branch became unreachable — a dead argument
# whose docstring still promised capacity was checked before any row was written. The
# pending list it capped is gone entirely now (enrolment is one step, §6). `None` from this
# helper therefore means "the config gates passed", not "the request is accepted"; the
# channel still has the structural check, the id charset check and the in-transaction
# collision gate ahead of it.
def test_enroll_all_ok_returns_none():
    assert enroll_reject_reason({"protocolVersion": 1}, 1, True, True) is None


def test_enroll_protocol_gates_before_window():
    # Wrong protocol wins even over a closed window and a bad code.
    assert (
        enroll_reject_reason({"protocolVersion": 9}, 1, False, False)
        == REJECT_PROTOCOL
    )


def test_enroll_closed_gates_before_bad_code():
    # A closed window wins over a bad code (order: window before code).
    assert (
        enroll_reject_reason({"protocolVersion": 1}, 1, False, False)
        == ENROLL_CLOSED
    )


def test_enroll_bad_code_is_the_last_gate_here():
    # The code is the LAST thing this helper decides — everything after it needs the DB.
    assert (
        enroll_reject_reason({"protocolVersion": 1}, 1, True, False)
        == ENROLL_BAD_CODE
    )


def test_enroll_reject_reason_takes_no_capacity_argument():
    """The pending-list gates are gone with the list; the signature must say so.

    Reddens if a ``has_capacity`` parameter is reintroduced — which is how an earlier
    version lied: the flag existed, the caller hardcoded it True, and a reader of the pure
    helper concluded the ceiling was enforced before any row was written. There is no
    ceiling now, and no pending row for one to bound.
    """
    import inspect

    params = list(inspect.signature(enroll_reject_reason).parameters)
    assert params == ["msg", "protocol_version", "window_open", "code_ok"]
    # The retired constants must not creep back: a reason with no producer is a metric
    # label the alert guard would have to excuse forever.
    assert not hasattr(protocol, "ENROLL_CAPACITY")
    assert not hasattr(protocol, "ENROLL_SECRET_CONFLICT")


def test_enroll_reason_constants_are_stable_strings():
    # The metric labels / wire strings must not drift (§12 alerts on bad_code and id_taken).
    assert (
        protocol.ENROLL_CLOSED,
        protocol.ENROLL_BAD_CODE,
        protocol.ENROLL_ID_TAKEN,
        protocol.ENROLL_BAD_ID,
    ) == ("closed", "bad_code", "id_taken", "bad_id")
    # `enroll_pending` is GONE, not renamed: a client that still waits for it would wait
    # forever instead of noticing it is already enrolled.
    assert not hasattr(protocol, "TYPE_ENROLL_PENDING")
    assert protocol.TYPE_ENROLL_ACCEPTED == "enroll_accepted"
    assert protocol.TYPE_ENROLL_REJECTED == "enroll_rejected"
    assert protocol.REJECT_REVOKED == "revoked"
    assert protocol.REJECT_UNKNOWN == "unknown_instance"


# --- instance_id_ok: the charset that becomes a PRIMARY KEY ------------------
def test_instance_id_charset_and_length():
    """The id the browser proposes becomes the row's PRIMARY KEY and travels into URLs,
    metric labels and the console, so the accepted set is exactly [A-Za-z0-9._-]{1,64}.

    This expression used to live in src/api/admin.py, applied to an id an OPERATOR typed
    into the console. It moved here because the id now arrives on an UNAUTHENTICATED /ext
    frame — the extension checks the same expression locally first, but that check is on
    the peer's side of the wire and cannot be the gate.
    """
    for good in ("main", "a", "A" * 64, "work-laptop", "Prox.2", "x_y-z.1"):
        assert instance_id_ok(good), good
    for bad in (
        "",              # blank
        "A" * 65,        # one over the ceiling
        "has space",     # the operator's most likely mistake
        "имя",           # non-ASCII
        "a/b",           # a path separator in something that lands in URLs
        "a:b",
        "a\nb",
        None,            # a missing field
        123,             # a non-str
        ["main"],
    ):
        assert not instance_id_ok(bad), repr(bad)


# --- the constants.js <-> protocol.py mirror (§6) ----------------------------
# The wire strings are DUPLICATED by construction: the extension is JavaScript loaded
# into a browser, the service is Python, and there is no shared artifact to import from.
# Both files say so in a comment ("Mirrors the service-side CMD_* strings" /
# "the two sides MUST agree"), which enforces nothing. Parsing the JS is the only way to
# turn that promise into a test — and this is exactly the class of divergence nothing
# else catches: adding a verb on one side alone type-checks, lints and passes every
# other test, then answers `internal: unknown command` on the owner's browser.
_CONSTANTS_JS = (
    Path(__file__).resolve().parent.parent / "extension" / "src" / "constants.js"
)
# `export const CMD_X = "…";` / `export const ERR_X = "…";` at the start of a line.
_JS_WIRE_CONST_RE = re.compile(
    r'^export const ((?:CMD|ERR)_[A-Z0-9_]+)\s*=\s*"([^"]*)";', re.MULTILINE
)

# The codes that exist only on the service side: no live socket for the instance, and no
# `response` frame inside the command budget. Both describe a MISSING peer, so the
# extension cannot possibly produce either and neither belongs in constants.js.
#
# ``ERR_TIMEOUT`` stays here even though this wave added waiting verbs (§11): a `wait_for`
# whose condition never came true answers `ok:true, matched:false` — the browser answered,
# the verdict is simply "no". Letting the extension ALSO say `timeout` would collapse that
# definite negative into "state unknown, do not retry", which is the one thing `timeout`
# must keep meaning.
_SERVICE_ONLY_ERRORS = {"ERR_NO_CONNECTION", "ERR_TIMEOUT"}


def _js_wire_constants() -> dict[str, str]:
    """``{NAME: value}`` for every ``CMD_*`` / ``ERR_*`` exported by constants.js."""
    found = dict(_JS_WIRE_CONST_RE.findall(_CONSTANTS_JS.read_text(encoding="utf-8")))
    # A moved/renamed file (or a reformatted export) would otherwise make every
    # assertion below vacuously true against an empty dict.
    assert found, f"parsed no CMD_*/ERR_* constants out of {_CONSTANTS_JS}"
    return found


def _py_wire_constants(prefix: str) -> dict[str, str]:
    return {
        name: value
        for name, value in vars(protocol).items()
        if name.startswith(prefix) and isinstance(value, str)
    }


def test_command_verbs_mirror_the_extension_exactly():
    """Every ``CMD_*`` name AND string is identical on both sides of the socket.

    Not a subset in either direction: a verb the service can send and the extension
    cannot execute answers `internal`, and a verb only the extension knows is dead code
    that reads as an implemented feature.
    """
    js = {k: v for k, v in _js_wire_constants().items() if k.startswith("CMD_")}
    assert js == _py_wire_constants("CMD_")
    # The seven original verbs plus move_tab, focus_window, the FIXED-function observation
    # verbs (get_text, wait_for, scroll_until, poll_job), the arbitrary-code start_js, the
    # first chrome.debugger verb set_focus_emulation, and the WebSocket-capture trio
    # (start_ws_capture / read_ws_frames / stop_ws_capture) — spelled out so a silent RENAME of
    # a live wire string (which would keep both sides equal, and break every deployed copy of
    # the other half) still reddens.
    assert set(js.values()) == {
        "open_tab", "close_tab", "get_tab", "focus_tab", "focus_window", "navigate_tab",
        "merge_windows", "execute_js", "move_tab", "get_text", "wait_for",
        "scroll_until", "start_js", "poll_job", "set_focus_emulation",
        "start_ws_capture", "read_ws_frames", "stop_ws_capture",
    }


def test_command_error_codes_mirror_the_extension():
    """Every ``ERR_*`` the extension can answer with exists here, spelled the same.

    The service side additionally owns ``no_connection`` / ``timeout`` — failures that
    happen when there is no peer to answer — and those two, exactly, may differ.
    """
    js = {k: v for k, v in _js_wire_constants().items() if k.startswith("ERR_")}
    py = _py_wire_constants("ERR_")
    assert set(py) - set(js) == _SERVICE_ONLY_ERRORS
    assert {k: v for k, v in py.items() if k not in _SERVICE_ONLY_ERRORS} == js
    # move_tab's pinned refusal is a code of its OWN, not `precondition_failed`: the
    # agent must be able to tell "the owner's do-not-touch shield stopped me" from every
    # other precondition without parsing a message string.
    assert protocol.ERR_PINNED_CROSS_WINDOW == "pinned_cross_window"
    assert js["ERR_PINNED_CROSS_WINDOW"] == protocol.ERR_PINNED_CROSS_WINDOW
    # set_focus_emulation's attach refusal is its own code too (DevTools open / another
    # debugger client), and the extension produces it, so it lives on BOTH sides identically.
    assert protocol.ERR_DEBUGGER_ATTACH == "debugger_attach"
    assert js["ERR_DEBUGGER_ATTACH"] == protocol.ERR_DEBUGGER_ATTACH
