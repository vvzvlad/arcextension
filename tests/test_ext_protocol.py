"""Pure-unit tests for the /ext protocol decision helpers (no socket, no DB).

``hello_reject_reason`` and ``enroll_reject_reason`` are deliberately pure so the reject
ORDER — which is load-bearing for enrollment acceptance 2/3 — can be pinned here without
a websocket. Each branch and the ordering between branches is asserted.
"""

from src.ext import protocol
from src.ext.protocol import (
    ENROLL_BAD_CODE,
    ENROLL_CAPACITY,
    ENROLL_CLOSED,
    REJECT_AUTH,
    REJECT_ORIGIN,
    REJECT_PROTOCOL,
    enroll_reject_reason,
    hello_reject_reason,
)


# --- hello_reject_reason (secret-based) --------------------------------------
def test_hello_protocol_mismatch_wins_first():
    # Even with a resolved id and matching origin, a wrong protocolVersion rejects first.
    msg = {"protocolVersion": 2, "origin": "chrome-extension://x"}
    assert hello_reject_reason(msg, 1, "i1", {"chrome-extension://x"}) == REJECT_PROTOCOL


def test_hello_none_resolved_id_is_auth():
    # The last-line guard: no active instance behind the secret => auth.
    msg = {"protocolVersion": 1}
    assert hello_reject_reason(msg, 1, None, set()) == REJECT_AUTH


def test_hello_empty_allowlist_accepts_any_origin():
    msg = {"protocolVersion": 1, "origin": "chrome-extension://anything"}
    assert hello_reject_reason(msg, 1, "i1", set()) is None


def test_hello_origin_rejected_when_not_in_nonempty_list():
    msg = {"protocolVersion": 1, "origin": "chrome-extension://evil"}
    assert hello_reject_reason(msg, 1, "i1", {"chrome-extension://good"}) == REJECT_ORIGIN


def test_hello_origin_ok_when_in_list():
    msg = {"protocolVersion": 1, "origin": "chrome-extension://good"}
    assert hello_reject_reason(msg, 1, "i1", {"chrome-extension://good"}) is None


def test_hello_no_token_field_consulted():
    # There is no longer any token in the decision — a hello WITHOUT a token still passes.
    msg = {"protocolVersion": 1}
    assert hello_reject_reason(msg, 1, "i1", set()) is None


# --- enroll_reject_reason: each branch + the load-bearing order --------------
# The helper owns the CONFIG-shaped gates ONLY (protocol -> window -> code). Capacity used
# to be a fifth parameter here; it moved into the write transaction
# (queries.upsert_enroll_request_capped) so it could be authoritative against racing
# enrolls, after which the only caller pinned `has_capacity=True` and the branch became
# unreachable — a dead argument whose docstring still promised capacity was checked before
# any row was written. `None` from this helper therefore means "the config gates passed",
# not "the request is accepted"; the channel still has the structural check and the
# in-transaction capacity/secret-conflict gates ahead of it.
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
    """The capacity gate is the write transaction's, and the signature must say so.

    Reddens if a ``has_capacity`` parameter is reintroduced — which is how the previous
    version lied: the flag existed, the caller hardcoded it True, and a reader of the pure
    helper concluded the ceiling was enforced before any row was written.
    """
    import inspect

    params = list(inspect.signature(enroll_reject_reason).parameters)
    assert params == ["msg", "protocol_version", "window_open", "code_ok"]
    # The constant survives — the CHANNEL returns it from the transaction's outcome.
    assert protocol.ENROLL_CAPACITY == "capacity"


def test_enroll_reason_constants_are_stable_strings():
    # The metric labels / wire strings must not drift (issue §37 alerts on bad_code).
    assert (protocol.ENROLL_CLOSED, protocol.ENROLL_BAD_CODE, protocol.ENROLL_CAPACITY) == (
        "closed",
        "bad_code",
        "capacity",
    )
    assert protocol.TYPE_ENROLL_PENDING == "enroll_pending"
    assert protocol.TYPE_ENROLL_REJECTED == "enroll_rejected"
    assert protocol.REJECT_REVOKED == "revoked"
    assert protocol.REJECT_UNKNOWN == "unknown_instance"
