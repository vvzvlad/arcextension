"""``/admin/*`` enrollment JSON API (issue #35 Task E) — the operator control surface.

Pins the acceptance rows the /admin JSON slice owns:

* (4)  approve → an active instances row with the request's secret_hash + the assigned
       id, the request row deleted, and ``resolve_secret`` now returning it active (a
       subsequent secret-hello would authenticate).
* (6)  every /admin endpoint rejects an INSTANCE secret with 401; ADMIN_TOKEN → 200.
* (9)  revoking MAIN without a matching replacement → 409; with ``replacement==MAIN`` →
       200; and approve RE-ACTIVATES the revoked MAIN (the round-trip).
* (11) two approves of one request resolve to exactly one 200 + one 409, leaving exactly
       one active instance / one secret; both 409 mechanisms (already-active WHERE guard
       and UNIQUE(secret_hash)) are exercised.
* (12) a request older than TTL is not returned by GET, and
       ``delete_expired_enroll_requests`` physically removes it; the sweep wiring runs it
       within TTL+TICK.
* revoke-404, approve-already-active-409, and admin_audit rows for approve/reject/revoke.

Each test is written to REDDEN on the specific mutation it guards (noted inline).
"""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from conftest import admin_headers, instance_headers, make_settings, secret_hash_for
from starlette.testclient import TestClient

from src.db.access import Database
from src.db.queries import ApproveConflict, approve_enroll_request


# --- low-level DB helpers ----------------------------------------------------
def _now() -> int:
    return int(time.time() * 1000)


def _seed_request(db_path, install_uuid, *, secret_hash, first_seen_at=None,
                  origin="chrome-extension://abc", title="T", proto=1):
    fs = first_seen_at if first_seen_at is not None else _now()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO enroll_requests (install_uuid, origin, suggested_title, "
            "protocol_version, secret_hash, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (install_uuid, origin, title, proto, secret_hash, fs, fs),
        )
        conn.commit()
    finally:
        conn.close()


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
        # An ACTIVE instance whose secret is a valid /api credential but NOT admin.
        _seed_instance(db_path, "inst", status="active",
                       secret_hash=secret_hash_for("inst"))
        inst = instance_headers(secret_hash_for("inst"))

        endpoints = [
            ("GET", "/admin/enroll/requests"),
            ("POST", "/admin/enroll/approve"),
            ("POST", "/admin/enroll/reject"),
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
        assert client.get("/admin/enroll/requests", headers=admin_headers()).status_code == 200
        assert client.get("/admin/instances", headers=admin_headers()).status_code == 200
        assert client.get("/admin/enroll/window", headers=admin_headers()).status_code == 200


# --- (4) approve activates the row with the request's secret -----------------
def test_approve_activates_instance_with_request_secret_and_consumes_request(tmp_path):
    """Acc 4: approve creates an ACTIVE instances row carrying the REQUEST's secret_hash
    under the operator-assigned id, deletes the request, and resolve_secret then returns
    it active (a later secret-hello would authenticate). An admin_audit 'approve' row is
    written. Reddens if approve stops copying secret_hash, stops deleting the request, or
    leaves status != 'active'."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    sh = "hash-of-secret-A"
    with TestClient(app) as client:
        _seed_request(db_path, "uuid-A", secret_hash=sh, title="Home")
        resp = client.post(
            "/admin/enroll/approve", headers=admin_headers(),
            json={"install_uuid": "uuid-A", "instance_id": "laptop"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"instance_id": "laptop", "status": "active"}

        rows = _q(db_path, "SELECT status, secret_hash, install_uuid, title, enrolled_at "
                           "FROM instances WHERE id='laptop'")
        assert len(rows) == 1
        status, secret_hash, install_uuid, title, enrolled_at = rows[0]
        assert status == "active" and secret_hash == sh
        assert install_uuid == "uuid-A" and title == "Home" and enrolled_at is not None
        # The request was consumed.
        assert _q(db_path, "SELECT 1 FROM enroll_requests WHERE install_uuid='uuid-A'") == []
        # resolve_secret now returns it active — a secret-hello would authenticate.
        assert asyncio.run(_resolve(db_path, sh)) == ("laptop", "active")
        # admin_audit recorded the approve.
        audit = _q(db_path, "SELECT action, install_uuid, instance_id, initiator "
                            "FROM admin_audit WHERE action='approve'")
        assert audit == [("approve", "uuid-A", "laptop", "admin")]


async def _resolve(db_path, secret_hash):
    from src.db.queries import resolve_secret
    db = Database(db_path, str(db_path) + ".bk")
    # Reuse the existing DB file; open() runs migrations (idempotent) on it.
    await db.open()
    try:
        return await db.read(lambda c: resolve_secret(c, secret_hash))
    finally:
        await db.close()


def test_approve_uses_body_title_over_suggested(tmp_path):
    """The body ``title`` overrides the client's suggested_title; absent, the suggested
    title is used. Reddens if the endpoint ignores the body title."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_request(db_path, "uuid-T", secret_hash="h-T", title="suggested")
        client.post("/admin/enroll/approve", headers=admin_headers(),
                    json={"install_uuid": "uuid-T", "instance_id": "i-T", "title": "chosen"})
        assert _q(db_path, "SELECT title FROM instances WHERE id='i-T'") == [("chosen",)]


def test_approve_missing_request_is_404(tmp_path):
    app = create_app_for(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/admin/enroll/approve", headers=admin_headers(),
                           json={"install_uuid": "ghost", "instance_id": "x"})
        assert resp.status_code == 404


# --- (11) two approves -> one 200 one 409, one active row / one secret --------
def test_two_approves_of_one_request_resolve_to_200_and_409(tmp_path):
    """Acc 11: given the request's secret captured once (both racing handlers read it
    before either write commits), two approves to the SAME id resolve to exactly one 200
    and one 409 via the ``WHERE status != 'active'`` guard, leaving ONE active row and ONE
    secret. Reddens if that WHERE guard is dropped (the second approve would overwrite and
    return 200 → two 'successes')."""
    db_path = str(tmp_path / "curator.db")

    async def _run():
        db = Database(db_path, str(tmp_path / "bk"))
        await db.open()
        try:
            # Seed the request; capture its secret once (as both handlers' read would).
            await db.write(lambda c: c.execute(
                "INSERT INTO enroll_requests (install_uuid, protocol_version, secret_hash, "
                "first_seen_at, last_seen_at) VALUES ('u', 1, 'sekret', 1, 1)"))
            outcomes = []
            for _ in range(2):
                try:
                    await db.write(lambda c: approve_enroll_request(
                        c, instance_id="one", secret_hash="sekret",
                        install_uuid="u", title=None, now=_now()))
                    outcomes.append(200)
                except ApproveConflict:
                    outcomes.append(409)
                except sqlite3.IntegrityError:
                    outcomes.append(409)
            return outcomes, await db.read(lambda c: c.execute(
                "SELECT COUNT(*) FROM instances WHERE status='active'").fetchone()[0]), \
                await db.read(lambda c: c.execute(
                "SELECT COUNT(*) FROM instances WHERE secret_hash='sekret'").fetchone()[0])
        finally:
            await db.close()

    outcomes, active_count, secret_count = asyncio.run(_run())
    assert sorted(outcomes) == [200, 409]
    assert active_count == 1 and secret_count == 1


def test_two_approves_to_different_ids_collide_on_unique_secret(tmp_path):
    """Acc 11, the OTHER 409 mechanism: two approves of one request to DIFFERENT ids —
    the second hits ``UNIQUE(secret_hash)`` (a second active instance carrying the same
    secret) and raises IntegrityError → 409. Reddens if the unique index is dropped (both
    would succeed, two actives share one secret)."""
    db_path = str(tmp_path / "curator.db")

    async def _run():
        db = Database(db_path, str(tmp_path / "bk"))
        await db.open()
        try:
            await db.write(lambda c: c.execute(
                "INSERT INTO enroll_requests (install_uuid, protocol_version, secret_hash, "
                "first_seen_at, last_seen_at) VALUES ('u', 1, 'sekret', 1, 1)"))
            await db.write(lambda c: approve_enroll_request(
                c, instance_id="idA", secret_hash="sekret", install_uuid="u",
                title=None, now=_now()))
            raised = None
            try:
                await db.write(lambda c: approve_enroll_request(
                    c, instance_id="idB", secret_hash="sekret", install_uuid="u",
                    title=None, now=_now()))
            except sqlite3.IntegrityError as e:
                raised = e
            n = await db.read(lambda c: c.execute(
                "SELECT COUNT(*) FROM instances WHERE secret_hash='sekret'").fetchone()[0])
            return raised, n
        finally:
            await db.close()

    raised, n = asyncio.run(_run())
    assert raised is not None
    assert n == 1


def test_approve_already_active_id_is_409(tmp_path):
    """A brand-new request whose operator-assigned id is ALREADY an active instance → 409
    (ApproveConflict), and that live instance is untouched. Reddens if the already-active
    guard is removed (the live instance would be overwritten with the new secret)."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "taken", status="active", secret_hash="old-secret",
                       install_uuid="old-uuid")
        _seed_request(db_path, "new-uuid", secret_hash="new-secret")
        resp = client.post("/admin/enroll/approve", headers=admin_headers(),
                           json={"install_uuid": "new-uuid", "instance_id": "taken"})
        assert resp.status_code == 409
        # The live instance kept its OWN secret; the request survives for a retry.
        assert _q(db_path, "SELECT secret_hash FROM instances WHERE id='taken'") == [("old-secret",)]
        assert _q(db_path, "SELECT 1 FROM enroll_requests WHERE install_uuid='new-uuid'") == [(1,)]


def test_approve_new_id_with_secret_of_active_instance_is_409_over_http(tmp_path):
    """acc-11 second mechanism, THROUGH the HTTP handler: approving a NEW id whose request
    carries a secret already held by an active instance → 409 via UNIQUE(secret_hash).
    Reddens if the handler's `except sqlite3.IntegrityError -> 409` branch is removed (the
    endpoint would 500 instead) — the DB-level tests do not cover this HTTP mapping."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "live", status="active", secret_hash="dup-secret",
                       install_uuid="live-uuid")
        _seed_request(db_path, "new-uuid", secret_hash="dup-secret")
        resp = client.post("/admin/enroll/approve", headers=admin_headers(),
                           json={"install_uuid": "new-uuid", "instance_id": "fresh-id"})
        assert resp.status_code == 409
        assert "secret is already enrolled" in resp.text  # string detail → PlainTextResponse
        # No second row created; the request survives for a retry.
        assert _q(db_path, "SELECT COUNT(*) FROM instances WHERE secret_hash='dup-secret'") == [(1,)]
        assert _q(db_path, "SELECT 1 FROM instances WHERE id='fresh-id'") == []
        assert _q(db_path, "SELECT 1 FROM enroll_requests WHERE install_uuid='new-uuid'") == [(1,)]


def test_approve_rejects_malformed_instance_id_400(tmp_path):
    """The operator-assigned instance_id (row PRIMARY KEY) is bounded charset/length;
    a space / overlong value → 400, no row written. Reddens if the _INSTANCE_ID_RE guard
    is removed."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_request(db_path, "u1", secret_hash="s1")
        for bad in ("has space", "x" * 65, "bad/slash"):
            resp = client.post("/admin/enroll/approve", headers=admin_headers(),
                               json={"install_uuid": "u1", "instance_id": bad})
            assert resp.status_code == 400, bad
        assert _q(db_path, "SELECT COUNT(*) FROM instances") == [(0,)]


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


# --- (9) revoke MAIN round-trip + re-approve restores it ---------------------
def test_revoke_main_409_without_replacement_then_reapprove_restores(tmp_path):
    """Acc 9: revoking MAIN without ``replacement==MAIN`` → 409; WITH it → 200; then an
    approve RE-ACTIVATES the revoked MAIN (the only restore path — Task D leaves MAIN
    revoked). Reddens if RevokeMainRefused stops mapping to 409, or if approve's
    ON CONFLICT reactivation (WHERE status!='active') is broken (the revoked MAIN could
    not be restored)."""
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

        # Re-approve restores MAIN to active with a fresh secret (the reactivation path).
        _seed_request(db_path, "main-uuid", secret_hash="main-new")
        restore = client.post("/admin/enroll/approve", headers=admin_headers(),
                              json={"install_uuid": "main-uuid", "instance_id": "main"})
        assert restore.status_code == 200
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


# --- reject: idempotent + audited -------------------------------------------
def test_reject_deletes_request_and_audits_idempotently(tmp_path):
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_request(db_path, "uuid-R", secret_hash="h-R")
        r1 = client.post("/admin/enroll/reject", headers=admin_headers(),
                         json={"install_uuid": "uuid-R"})
        assert r1.status_code == 200 and r1.json()["deleted"] is True
        assert _q(db_path, "SELECT 1 FROM enroll_requests WHERE install_uuid='uuid-R'") == []
        # Idempotent: a second reject is still 200, deleted=False.
        r2 = client.post("/admin/enroll/reject", headers=admin_headers(),
                         json={"install_uuid": "uuid-R"})
        assert r2.status_code == 200 and r2.json()["deleted"] is False
        assert _q(db_path, "SELECT COUNT(*) FROM admin_audit WHERE action='reject'") == [(2,)]


# --- (12) TTL: read filter + physical delete + sweep wiring ------------------
def test_ttl_read_filter_hides_expired_but_keeps_row(tmp_path):
    """Acc 12 part 1: the read-time filter (``list_pending_enroll_requests``) never returns
    a request whose frozen first_seen_at is older than the TTL, while the row is STILL
    PHYSICALLY PRESENT (read filter and physical delete are independent). Tested against
    the DB directly so the live background sweep cannot race the physical-presence check.
    Reddens if the read drops the ``first_seen_at >= now-ttl`` filter (the stale request
    would appear) — and separately proves the row is only hidden, not yet deleted."""
    from src.db.queries import list_pending_enroll_requests

    db_path = str(tmp_path / "curator.db")

    async def _run():
        db = Database(db_path, str(tmp_path / "bk"))
        await db.open()
        try:
            now = _now()
            _seed_request(db_path, "freshie", secret_hash="h1", first_seen_at=now)
            _seed_request(db_path, "staleee", secret_hash="h2",
                          first_seen_at=now - 61 * 60_000)  # 61 min old > 60 min TTL
            rows = await db.read(lambda c: list_pending_enroll_requests(
                c, now=now, ttl_ms=60 * 60_000))
            physical = await db.read(lambda c: c.execute(
                "SELECT COUNT(*) FROM enroll_requests").fetchone()[0])
            return rows, physical
        finally:
            await db.close()

    rows, physical = asyncio.run(_run())
    assert {r["install_uuid"] for r in rows} == {"freshie"}
    assert physical == 2  # only filtered at read time, not deleted
    assert rows[0]["install_uuid_short"] == "freshie"[:8]


def test_delete_expired_enroll_requests_physical_removal(tmp_path):
    """Acc 12 part 2: the pure ``delete_expired_enroll_requests(conn, cutoff)`` removes
    rows with first_seen_at < cutoff and KEEPS newer ones. Reddens if the boundary flips
    (it would delete the fresh row or keep the stale one)."""
    from src.db.queries import list_pending_enroll_requests
    from src.db.retention import delete_expired_enroll_requests, enroll_request_cutoff_ms

    db_path = str(tmp_path / "curator.db")

    async def _run():
        db = Database(db_path, str(tmp_path / "bk"))
        await db.open()
        try:
            now = _now()
            _seed_request(db_path, "fresh", secret_hash="h1", first_seen_at=now)
            _seed_request(db_path, "stale", secret_hash="h2", first_seen_at=now - 61 * 60_000)
            cutoff = enroll_request_cutoff_ms(now, 60)
            deleted = await db.write(lambda c: delete_expired_enroll_requests(c, cutoff))
            remaining = await db.read(
                lambda c: [r["install_uuid"]
                           for r in list_pending_enroll_requests(c, now=now, ttl_ms=60 * 60_000)])
            all_rows = await db.read(
                lambda c: [r[0] for r in c.execute(
                    "SELECT install_uuid FROM enroll_requests").fetchall()])
            return deleted, remaining, all_rows
        finally:
            await db.close()

    deleted, remaining, all_rows = asyncio.run(_run())
    assert deleted == 1
    assert remaining == ["fresh"] and all_rows == ["fresh"]


def test_sweep_loop_deletes_within_ttl_plus_tick(tmp_path):
    """Acc 12 part 3: the wired ``enroll_request_sweep_loop`` physically deletes an expired
    request on its next tick — proving the sweep is the mechanism that bounds physical
    removal at TTL+TICK. Driven with a tiny interval so the test is fast. Reddens if the
    loop stops calling delete_expired_enroll_requests."""
    from src.db.retention import enroll_request_sweep_loop

    db_path = str(tmp_path / "curator.db")

    async def _run():
        db = Database(db_path, str(tmp_path / "bk"))
        await db.open()
        try:
            now = _now()
            _seed_request(db_path, "stale", secret_hash="h", first_seen_at=now - 61 * 60_000)
            _seed_request(db_path, "fresh", secret_hash="h2", first_seen_at=now)
            # ttl_min=60, interval_ms tiny (clamped to 1s min inside; use the clamp).
            task = asyncio.create_task(enroll_request_sweep_loop(db, 60, 1))
            # Wait for the stale row to disappear (bounded).
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                rows = await db.read(lambda c: c.execute(
                    "SELECT install_uuid FROM enroll_requests ORDER BY install_uuid").fetchall())
                if [r[0] for r in rows] == ["fresh"]:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return [r[0] for r in await db.read(lambda c: c.execute(
                "SELECT install_uuid FROM enroll_requests ORDER BY install_uuid").fetchall())]
        finally:
            await db.close()

    assert asyncio.run(_run()) == ["fresh"]


# --- window endpoints + degraded gating -------------------------------------
def test_window_open_read_close_and_audit(tmp_path):
    """Arm → GET reports open with the code → DELETE closes it; window_open/window_close
    audit rows are written. Reddens if the code is not returned while open, or the audit
    is dropped."""
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
        assert client.get("/admin/enroll/requests", headers=admin_headers()).status_code == 200
        assert client.get("/admin/instances", headers=admin_headers()).status_code == 200
        assert client.get("/admin/enroll/window", headers=admin_headers()).status_code == 200
        # writes 503
        assert client.post("/admin/enroll/approve", headers=admin_headers(),
                           json={"install_uuid": "u", "instance_id": "i"}).status_code == 503
        assert client.post("/admin/enroll/reject", headers=admin_headers(),
                           json={"install_uuid": "u"}).status_code == 503
        assert client.post("/admin/instances/x/revoke", headers=admin_headers(),
                           json={}).status_code == 503
        assert client.post("/admin/enroll/window", headers=admin_headers()).status_code == 503
        assert client.delete("/admin/enroll/window", headers=admin_headers()).status_code == 503


def test_list_instances_shows_all_statuses(tmp_path):
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "a", status="active", secret_hash="sa")
        _seed_instance(db_path, "b", status="revoked", secret_hash="sb")
        _seed_instance(db_path, "c", status="pending", secret_hash="sc")
        got = client.get("/admin/instances", headers=admin_headers()).json()["instances"]
        by_id = {i["id"]: i["status"] for i in got}
        assert by_id == {"a": "active", "b": "revoked", "c": "pending"}


# --- app factory helper ------------------------------------------------------
def create_app_for(tmp_path, **over):
    from src.app import create_app
    return create_app(make_settings(tmp_path, pass_interval_min=100_000, **over))
