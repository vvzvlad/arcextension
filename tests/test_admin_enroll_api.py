"""``/admin/*`` — the operator control surface that is LEFT after enrolment lost its
second step (§6, §13).

What this file no longer covers, and why: there is no ``/admin/enroll/approve``,
``/admin/enroll/reject`` or ``/admin/enroll/requests``. A browser enrols itself over /ext
by presenting a valid window code and the id it wants, so there is no pending row to list,
approve or reject, and no TTL to sweep. Those frames are tested in
``tests/test_ext_channel.py`` and the SQL that writes them in ``tests/test_revoke.py``
(``enroll_instance``).

What remains here:

* (6)  every /admin endpoint rejects an INSTANCE secret with 401; ADMIN_TOKEN → 200.
* (9)  revoking MAIN without a matching replacement → 409; with ``replacement==MAIN`` →
       200 — and the id it frees can be taken again (the restore path, asserted at the
       query layer since the operator no longer performs it).
* the enrollment window: arm → read (with the live code) → close, each audited.
* degraded gating: mutating verbs 503, reads still available.
* the instances list: all statuses, and a revoked row never reads as connected.

Each test is written to REDDEN on the specific mutation it guards (noted inline).
"""

import sqlite3
import time

from conftest import (
    admin_headers,
    instance_headers,
    make_settings,
    secret_for,
    secret_hash_for,
)
from starlette.testclient import TestClient

from src.db.queries import ENROLL_OK, enroll_instance


# --- low-level DB helpers ----------------------------------------------------
def _now() -> int:
    return int(time.time() * 1000)


def _seed_instance(db_path, iid, *, status, secret_hash=None, install_uuid=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO instances (id, status, secret_hash, install_uuid, connected) "
            "VALUES (?, ?, ?, ?, 0)",
            (iid, status, secret_hash, install_uuid),
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


# --- (6) admin-only auth -----------------------------------------------------
def test_every_admin_endpoint_rejects_instance_and_anon(tmp_path):
    """Acc 6: an INSTANCE secret (and no token) is 401 on EVERY /admin route; ADMIN_TOKEN
    passes the gate. Reddens if ``require_admin`` stops narrowing to kind=='admin' (an
    instance caller would then reach the handler)."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # An ACTIVE instance whose secret is a valid /api credential but NOT admin. The
        # stored secret_hash is sha256(raw); the /api Bearer is the RAW secret (option A).
        _seed_instance(db_path, "inst", status="active",
                       secret_hash=secret_hash_for("inst"))
        inst = instance_headers(secret_for("inst"))

        endpoints = [
            ("GET", "/admin/instances"),
            ("POST", "/admin/instances/inst/revoke"),
            ("POST", "/admin/enroll/window"),
            ("GET", "/admin/enroll/window"),
            ("DELETE", "/admin/enroll/window"),
        ]
        for method, path in endpoints:
            r_inst = client.request(method, path, headers=inst, json={})
            assert r_inst.status_code == 401, f"{method} {path} instance -> {r_inst.status_code}"
            r_anon = client.request(method, path, json={})
            assert r_anon.status_code == 401, f"{method} {path} anon -> {r_anon.status_code}"

        # ADMIN_TOKEN passes the gate on the read endpoints (200).
        assert client.get("/admin/instances", headers=admin_headers()).status_code == 200
        assert client.get("/admin/enroll/window", headers=admin_headers()).status_code == 200


def test_the_approval_endpoints_are_gone(tmp_path):
    """The retired routes must 404, not linger as a second way in.

    A route left mounted over a handler nobody maintains is exactly how the two-step flow
    would come back by accident — and, worse, an ``/admin/enroll/approve`` that still
    answered would be an ADMIN-authenticated way to mint an instance outside the window
    gate. Reddens if any of them is re-registered.
    """
    app = create_app_for(tmp_path)
    with TestClient(app) as client:
        for method, path in [
            ("GET", "/admin/enroll/requests"),
            ("POST", "/admin/enroll/approve"),
            ("POST", "/admin/enroll/reject"),
        ]:
            resp = client.request(method, path, headers=admin_headers(), json={})
            assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"


# --- the instances list ------------------------------------------------------
def test_list_instances_reports_revoked_as_not_connected(tmp_path):
    """A revoked row leaves the stale `connected` column set (teardown is async); the
    operator console must still show it disconnected. Reddens if the status gate on
    `connected` is removed."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "gone", status="revoked", secret_hash=None,
                       install_uuid="gone-uuid")
        _c = sqlite3.connect(db_path)
        try:
            _c.execute("UPDATE instances SET connected=1 WHERE id='gone'")
            _c.commit()
        finally:
            _c.close()
        resp = client.get("/admin/instances", headers=admin_headers())
        assert resp.status_code == 200
        row = next(i for i in resp.json()["instances"] if i["id"] == "gone")
        assert row["status"] == "revoked" and row["connected"] is False


def test_list_instances_names_the_configured_main(tmp_path):
    """The list says WHICH id is MAIN, and says it even when that id has no row.

    MAIN is service configuration (``MAIN_INSTANCE_ID``), not a column: nothing in a row
    distinguishes it, so a client reading this endpoint could not mark the MAIN row nor warn
    about a MAIN revoke BEFORE sending it — its only way to discover the fact was to fire
    the revoke and read the 409 back, i.e. to find out by doing the thing it was meant to
    warn about. Reddens if the key is dropped from the body, or if it is derived from the
    rows (the second half below: a configured MAIN that has not enrolled yet — the state
    every fresh deployment starts in — still has to be named).
    """
    app = create_app_for(tmp_path, main_instance_id="chief")
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "chief", status="active", secret_hash="s-chief")
        _seed_instance(db_path, "work", status="active", secret_hash="s-work")
        body = client.get("/admin/instances", headers=admin_headers()).json()
        assert body["main_instance_id"] == "chief"
        assert {i["id"] for i in body["instances"]} == {"chief", "work"}

    # …and with no row for it at all, the configuration is still reported.
    empty = tmp_path / "empty"
    empty.mkdir()
    app2 = create_app_for(empty, main_instance_id="chief")
    with TestClient(app2) as client:
        body = client.get("/admin/instances", headers=admin_headers()).json()
        assert body["instances"] == []
        assert body["main_instance_id"] == "chief"


def test_window_read_reports_the_configured_length(tmp_path):
    """A CLOSED window still reports how long the next one will last.

    ``seconds_remaining`` is 0 while closed and there is no deadline to subtract from, so
    the configured length is not derivable from this body — yet it is exactly what the
    console's "Открыть регистрацию на N минут" button has to name. Reddens if
    ``window_minutes`` is dropped or stops following ENROLL_WINDOW_MIN (the button would go
    back to a hard-coded 10 and lie on any deployment that set the variable).
    """
    app = create_app_for(tmp_path, enroll_window_min=25)
    with TestClient(app) as client:
        closed = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert closed["open"] is False and closed["window_minutes"] == 25
        client.post("/admin/enroll/window", headers=admin_headers())
        opened = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert opened["open"] is True and opened["window_minutes"] == 25


def test_list_instances_shows_all_statuses_and_no_title(tmp_path):
    """Every status is listed — and the row carries NO ``title`` field.

    The id IS the name (§6): the column is dropped in migration 3 and the console prints
    the id. Reddens if a ``title`` key is reintroduced into the JSON, which is how a
    half-removed field starts being re-populated by the next writer.
    """
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "a", status="active", secret_hash="sa")
        _seed_instance(db_path, "b", status="revoked", secret_hash="sb")
        got = client.get("/admin/instances", headers=admin_headers()).json()["instances"]
        by_id = {i["id"]: i["status"] for i in got}
        assert by_id == {"a": "active", "b": "revoked"}
        for row in got:
            assert "title" not in row


# --- (9) revoke MAIN round-trip + the restore path ---------------------------
def test_revoke_main_409_without_replacement_then_the_id_can_be_retaken(tmp_path):
    """Acc 9: revoking MAIN without ``replacement==MAIN`` → 409; WITH it → 200.

    Then the RESTORE: the freed id is taken again by an enrolment, which is the only way
    back for a revoked MAIN (migration 2 leaves every pre-enrolment row revoked). That
    half is asserted at the query layer because no operator action performs it anymore —
    the browser re-enrols itself through /ext. Reddens if ``RevokeMainRefused`` stops
    mapping to 409, or if ``enroll_instance``'s reactivation (``WHERE status!='active'``)
    breaks — the revoked MAIN could then never come back.
    """
    app = create_app_for(tmp_path, main_instance_id="main")
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "main", status="active", secret_hash="main-old")
        # Without replacement → 409.
        assert client.post("/admin/instances/main/revoke", headers=admin_headers(),
                           json={}).status_code == 409
        # Wrong replacement → 409.
        assert client.post("/admin/instances/main/revoke", headers=admin_headers(),
                           json={"replacement": "other"}).status_code == 409
        assert _q(db_path, "SELECT status FROM instances WHERE id='main'") == [("active",)]
        # replacement == MAIN → 200, status revoked.
        ok = client.post("/admin/instances/main/revoke", headers=admin_headers(),
                         json={"replacement": "main"})
        assert ok.status_code == 200 and ok.json() == {"instance_id": "main", "status": "revoked"}
        assert _q(db_path, "SELECT status FROM instances WHERE id='main'") == [("revoked",)]

    # The restore: a fresh enrolment reclaims the revoked id with a NEW secret.
    conn = sqlite3.connect(db_path)
    try:
        outcome = enroll_instance(
            conn, instance_id="main", secret_hash="main-new",
            install_uuid="main-uuid", now=_now(),
        )
        conn.commit()
    finally:
        conn.close()
    assert outcome == ENROLL_OK
    assert _q(db_path, "SELECT status, secret_hash FROM instances WHERE id='main'") \
        == [("active", "main-new")]


def test_revoke_nonexistent_is_404(tmp_path):
    """Revoking an id with no row → 404 (RevokeResult.revoked is False). Reddens if the
    no-row case is not mapped to 404 (it would answer 200 for a phantom)."""
    app = create_app_for(tmp_path)
    with TestClient(app) as client:
        assert client.post("/admin/instances/ghost/revoke", headers=admin_headers(),
                           json={}).status_code == 404


def test_revoke_writes_audit_and_closes_socket(tmp_path):
    """A successful revoke writes an admin_audit 'revoke' row AND best-effort closes the
    live socket via the registry. Reddens if the audit is dropped or the socket is left
    open."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "gone", status="active", secret_hash="s-gone")

        # Register a fake live socket for 'gone'.
        from src.ext.registry import ConnState

        class _WS:
            def __init__(self):
                self.closed = False

            async def close(self, code=1000):
                self.closed = True

        ws = _WS()
        client.app.state.ext_registry.put(
            "gone", ConnState(ws=ws, conn_epoch=1, install_uuid="u", session_id="s"))

        resp = client.post("/admin/instances/gone/revoke", headers=admin_headers(), json={})
        assert resp.status_code == 200
        assert ws.closed is True
        assert client.app.state.ext_registry.get("gone") is None
        audit = _q(db_path, "SELECT action, instance_id, initiator FROM admin_audit "
                            "WHERE action='revoke'")
        assert audit == [("revoke", "gone", "admin")]


# --- window endpoints + degraded gating -------------------------------------
def test_window_open_read_close_and_audit(tmp_path):
    """Arm → GET reports open with the code → DELETE closes it; window_open/window_close
    audit rows are written. Reddens if the code is not returned while open, or the audit
    is dropped.

    The window carries MORE weight than it used to: it is now the WHOLE permission to
    enrol, not merely the interval an approval had to fall inside. Arming it is therefore
    the operator's one deliberate act, and `window_open` is the audit row that records it.
    """
    app = create_app_for(tmp_path, enroll_window_min=10)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        opened = client.post("/admin/enroll/window", headers=admin_headers())
        assert opened.status_code == 200
        code = opened.json()["code"]
        assert code and opened.json()["seconds_remaining"] > 0

        state = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert state["open"] is True and state["code"] == code

        assert client.delete("/admin/enroll/window", headers=admin_headers()).status_code == 200
        closed = client.get("/admin/enroll/window", headers=admin_headers()).json()
        assert closed["open"] is False and "code" not in closed

        actions = {r[0] for r in _q(db_path, "SELECT action FROM admin_audit")}
        assert {"window_open", "window_close"} <= actions


def test_writes_503_in_degraded_reads_ok(tmp_path):
    """Mutating /admin verbs answer 503 while degraded; reads stay available. Reddens if a
    mutating verb drops ``require_operational`` (it would try to write on a degraded DB)."""
    app = create_app_for(tmp_path)
    with TestClient(app) as client:
        client.app.state.degraded = True
        # reads OK
        assert client.get("/admin/instances", headers=admin_headers()).status_code == 200
        assert client.get("/admin/enroll/window", headers=admin_headers()).status_code == 200
        # writes 503
        assert client.post("/admin/instances/x/revoke", headers=admin_headers(),
                           json={}).status_code == 503
        assert client.post("/admin/enroll/window", headers=admin_headers()).status_code == 503
        assert client.delete("/admin/enroll/window", headers=admin_headers()).status_code == 503


# --- app factory helper ------------------------------------------------------
def create_app_for(tmp_path, **over):
    from src.app import create_app
    return create_app(make_settings(tmp_path, pass_interval_min=100_000, **over))
