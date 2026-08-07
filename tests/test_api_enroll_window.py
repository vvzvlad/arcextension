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
* the AUDIT distinguishes the two callers: 'user' for the startpage, 'admin' for
  ADMIN_TOKEN. This is the one signal an operator has that a window was armed from a
  browser rather than from the console, and it reddens if ``initiator_for(caller)`` is
  ever replaced by a hard-coded "admin";
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


# --- the audit trail ---------------------------------------------------------
def test_audit_initiator_tells_the_startpage_apart_from_the_console(tmp_path):
    """An instance-armed window audits as 'user'; an ADMIN_TOKEN-armed one as 'admin'.

    THE pin on the trade-off this verb accepts (see src/api/enroll.py): letting an
    instance secret arm a window is only defensible while the trail says WHICH kind of
    caller did it. Both halves are asserted, so replacing ``initiator_for(caller)`` with a
    hard-coded ``"admin"`` reddens on the first assertion and a hard-coded ``"user"`` on
    the second.
    """
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        headers = _active_instance(db_path)
        assert client.post("/api/enroll/window", headers=headers).status_code == 200
        rows = _q(db_path, "SELECT action, initiator FROM admin_audit ORDER BY id")
        assert rows == [("window_open", "user")]

        assert client.post("/api/enroll/window", headers=admin_headers()).status_code == 200
        rows = _q(db_path, "SELECT action, initiator FROM admin_audit ORDER BY id")
        assert rows == [("window_open", "user"), ("window_open", "admin")]


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
