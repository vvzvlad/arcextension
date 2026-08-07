"""``POST /api/enroll/window`` — the startpage's own way to arm the enrollment window (§13).

The startpage button «Открыть регистрацию и скопировать код» does two things in ONE click:
it arms the window and puts the freshly minted code on the clipboard. The clipboard half
forces the first half: ``navigator.clipboard.writeText`` needs the clicking document's
user activation, so the code must come back to the PAGE — and the page authenticates with
the raw INSTANCE secret, which ``/admin`` rejects by design. Hence this second door onto
the same core.

What each test pins:

* an INSTANCE secret really opens the window (the whole reason the verb exists), and the
  code it returns is the code the SERVER now holds — asserted through the server's own
  read path, not through the response we just parsed;
* ADMIN_TOKEN works too — this is not an instance-only verb;
* anonymous / garbage Bearer → 401;
* a REVOKED or still-PENDING instance secret → 401, and nothing is armed. This is the
  boundary between "an accepted trade-off" (an active instance may arm a window) and
  "revoke does not work";
* the AUDIT distinguishes the two callers: 'user' for the startpage, 'admin' for
  ADMIN_TOKEN, and it names WHICH instance armed it. This is the one signal an operator
  has that a window was armed from a browser rather than from the console, and it reddens
  if ``initiator_for(caller)`` is ever replaced by a hard-coded "admin" or if the
  ``instance_id`` stops being written;
* the response carrying the LIVE code says ``Cache-Control: no-store``;
* the stop gate is deliberately ABSENT: arming works while the curator is stopped;
* degraded mode refuses the write (503);
* every open mints a FRESH code, so a previous window's code cannot be re-used.
"""

import sqlite3

from conftest import (
    admin_headers,
    instance_headers,
    make_settings,
    secret_for,
    secret_hash_for,
)
from starlette.testclient import TestClient


# --- helpers -----------------------------------------------------------------
def create_app_for(tmp_path, **over):
    from src.app import create_app

    return create_app(make_settings(tmp_path, pass_interval_min=100_000, **over))


def _seed_instance(db_path, iid, *, status="active", secret_hash=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO instances (id, status, secret_hash, connected) VALUES (?, ?, ?, 0)",
            (iid, status, secret_hash),
        )
        conn.commit()
    finally:
        conn.close()


def _q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _active_instance(db_path, iid="startpage"):
    """An ACTIVE row whose stored hash is sha256(raw); the /api Bearer is the RAW secret."""
    _seed_instance(db_path, iid, status="active", secret_hash=secret_hash_for(iid))
    return instance_headers(secret_for(iid))


# --- the verb itself ---------------------------------------------------------
def test_instance_secret_arms_the_window(tmp_path):
    """The startpage's own credential opens the window and gets the code back.

    Reddens if the route is gated on ADMIN (``require_admin`` instead of
    ``require_api_caller``) — the button would 401 on every press.
    """
    app = create_app_for(tmp_path, enroll_window_min=10)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        resp = client.post("/api/enroll/window", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["code"], str) and len(body["code"]) == 6
        assert body["seconds_remaining"] > 0
        assert body["until"] > 0


def test_the_live_code_response_is_never_cached(tmp_path):
    """The body carries the LIVE window code — the whole permission to enrol — so it must
    say ``no-store``.

    ``AdminSecurityHeadersMiddleware`` puts that directive on ``GET /admin/enroll/window``
    for exactly this payload, but it matches on the ``/admin`` prefix and this route is
    under ``/api``, so the header has to be set by the handler. Without it the default
    cache heuristics let a browser or an intermediary write the code to disk, where it
    outlives both the window and the session. ``no-cache`` would NOT do: it still permits
    a stored copy.
    """
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        resp = client.post("/api/enroll/window", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"


def test_the_returned_code_is_the_one_the_server_holds(tmp_path):
    """The code the page copies is the code an enrolling browser must type.

    Asserted through the SERVER's own read path (``GET /admin/enroll/window``), not by
    re-reading the response we already have: a handler that minted a code and forgot to
    persist it would still answer 200 with a plausible-looking body, and the human would
    type a code nothing accepts.
    """
    app = create_app_for(tmp_path, enroll_window_min=10)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        code = client.post("/api/enroll/window", headers=headers).json()["code"]

        state = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert state["open"] is True
        assert state["code"] == code


def test_admin_token_also_arms_it(tmp_path):
    """Not an instance-only verb: ``require_api_caller`` accepts ADMIN_TOKEN too, so curl
    and the MCP agent reach the same core through the same door. Reddens if the handler
    starts narrowing to ``kind == 'instance'``."""
    app = create_app_for(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/api/enroll/window", headers=admin_headers())
        assert resp.status_code == 200
        assert resp.json()["code"]


def test_missing_or_garbage_bearer_is_401(tmp_path):
    """No credential and an unknown one are the same flat 401 — an anonymous caller must
    not be able to arm an enrollment window, nor to tell a wrong token from no token."""
    app = create_app_for(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/enroll/window").status_code == 401
        assert client.post(
            "/api/enroll/window", headers={"Authorization": "Bearer nope-not-a-secret"}
        ).status_code == 401
        # …and nothing was armed by the refused attempts.
        state = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert state["open"] is False


def test_a_revoked_or_pending_secret_cannot_arm_anything(tmp_path):
    """A secret whose row is NOT active opens nothing — 401, and no window.

    This is THE boundary the trade-off in ``src/api/enroll.py`` rests on. Letting an
    instance secret arm a window is defensible only while a revoke really takes that power
    away: if a revoked secret could still arm, the window it opens would let its holder
    enrol a fresh identity, and revoke would be decoration. ``pending`` is the same gate
    from the other side — a row that has not been admitted yet must not act like one that
    has. ``require_api_caller`` matches ONLY ``status='active'``; this reddens the moment
    that narrowing is lost.
    """
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        for iid, status in (("gone", "revoked"), ("waiting", "pending")):
            _seed_instance(db_path, iid, status=status, secret_hash=secret_hash_for(iid))
            resp = client.post(
                "/api/enroll/window", headers=instance_headers(secret_for(iid))
            )
            assert resp.status_code == 401, f"{status} secret must not arm a window"

        # Asserted through the server's own read path: a handler that armed and THEN
        # refused would leave a live window behind a 401.
        state = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert state["open"] is False


# --- the stop gate is deliberately absent ------------------------------------
def test_arming_works_while_the_curator_is_stopped(tmp_path):
    """The emergency stop does NOT gate this verb, and that is a decision, not an oversight.

    §7's stop silences the curator's AUTOMATION; enrolling a browser is an operator action,
    so it stays legal while stopped — which also means the startpage button keeps working
    exactly when the human most needs a way in. Nothing else pins this: without the test,
    adding ``require_not_paused`` here (an easy "for consistency" edit) would make the
    button answer 423 while the stop is on, silently, and the module docstring would go
    stale with no test reddening.
    """
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        assert client.post("/api/pause", headers=headers).status_code == 200

        resp = client.post("/api/enroll/window", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["code"]
        # The window really is open — not a 200 over a no-op.
        state = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert state["open"] is True


# --- the audit trail ---------------------------------------------------------
def test_audit_initiator_tells_the_startpage_apart_from_the_console(tmp_path):
    """An instance-armed window audits as 'user' AND names the instance; an ADMIN_TOKEN-armed
    one as 'admin' with no instance.

    THE pin on the trade-off this verb accepts (see src/api/enroll.py): letting an
    instance secret arm a window is only defensible while the trail says WHICH kind of
    caller did it — and WHICH browser. Both halves are asserted, so replacing
    ``initiator_for(caller)`` with a hard-coded ``"admin"`` reddens on the first assertion
    and a hard-coded ``"user"`` on the second. ``instance_id`` is asserted with them: the
    column exists and the caller carries the id, and without it an operator investigating a
    stray enrollment reads "some browser armed a window" and cannot tell whose secret
    leaked. It is ``None`` for an ADMIN_TOKEN caller, which is not an instance at all.
    """
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        assert client.post("/api/enroll/window", headers=headers).status_code == 200
        rows = _q(
            db_path, "SELECT action, initiator, instance_id FROM admin_audit ORDER BY id"
        )
        assert rows == [("window_open", "user", "startpage")]

        assert client.post("/api/enroll/window", headers=admin_headers()).status_code == 200
        rows = _q(
            db_path, "SELECT action, initiator, instance_id FROM admin_audit ORDER BY id"
        )
        assert rows == [
            ("window_open", "user", "startpage"),
            ("window_open", "admin", None),
        ]


# --- degraded mode -----------------------------------------------------------
def test_degraded_mode_refuses_the_write(tmp_path):
    """A migration failure means the schema cannot be trusted for authoritative writes, and
    arming a window IS one. Reddens if ``require_operational`` is dropped from the handler."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        client.app.state.degraded = True
        assert client.post("/api/enroll/window", headers=headers).status_code == 503
        assert client.post("/api/enroll/window", headers=admin_headers()).status_code == 503


# --- freshness ---------------------------------------------------------------
def test_two_arms_mint_different_codes(tmp_path):
    """Every open mints a FRESH code (slice A): a previous window's code is dead the moment
    a new one is armed. Reddens if the handler ever starts re-using a live code — the human
    would copy a string that some earlier screenshot still shows as valid."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        first = client.post("/api/enroll/window", headers=headers).json()["code"]
        second = client.post("/api/enroll/window", headers=headers).json()["code"]
        assert first != second
        # The server holds the SECOND one — the first is no longer accepted anywhere.
        state = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert state["code"] == second
