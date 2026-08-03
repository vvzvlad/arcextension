"""Pure-unit tests for the /ext protocol decision helpers (no socket, no DB).

``hello_reject_reason`` and ``enroll_reject_reason`` are deliberately pure so the reject
ORDER — which is load-bearing for enrollment acceptance 2/3 — can be pinned here without
a websocket. Each branch and the ordering between branches is asserted.
"""

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
