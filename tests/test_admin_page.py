"""``/admin`` HTML console (issue #36) — the cookie-session + CSP + CSRF layer over the
#35 JSON API. Each test maps a numbered acceptance row and is written to REDDEN on the
specific invariant it guards (noted inline).

* (1) GET /admin with no cookie AND no Bearer → 401, no ADMIN_TOKEN leaked in the body.
* (2) POST /admin/login (correct token) → 200 + a hardened Set-Cookie whose VALUE is a
      random id, NOT the ADMIN_TOKEN.
* (3) cookie-authenticated mutating request with a FOREIGN Origin → 403 (CSRF); a Bearer
      request with the same foreign Origin → NOT 403 (Bearer skips CSRF); same-origin cookie
      mutation → allowed.
* (4) rotating ADMIN_TOKEN (fingerprint mismatch) invalidates an already-issued cookie.
* (5) POST /admin/logout drops the session SERVER-side (a replayed cookie no longer opens).
* (6) an enroll request with an XSS payload is served verbatim by the JSON API and the page
      JS renders untrusted fields with textContent (never innerHTML).
* (7) a Bearer ADMIN_TOKEN opens /admin and every /admin JSON endpoint even once a cookie
      exists, and takes precedence over an ambient cookie (no CSRF).
* (8) degraded: GET /admin (and a JSON read) still respond; a mutating verb → 503.
* session expiry (past TTL → invalid) and login with a wrong token → 401 (no cookie).
"""

from conftest import ADMIN_TOKEN, admin_headers, make_settings, secret_hash_for
from starlette.testclient import TestClient

from src.api import admin_session


def create_app_for(tmp_path, **over):
    from src.app import create_app
    return create_app(make_settings(tmp_path, pass_interval_min=100_000, **over))


def _tc(app) -> TestClient:
    """A TestClient driven over HTTPS. The session cookie is ``Secure`` (set unconditionally
    because TLS terminates at Traefik), and http.cookiejar refuses to send a Secure cookie
    over http — so the browser-facing scheme must be https for the jar to replay it."""
    return TestClient(app, base_url="https://testserver")


# --- low-level seed of a pending enroll request ------------------------------
def _seed_request(db_path, install_uuid, *, secret_hash, title="T",
                  origin="chrome-extension://abc", proto=1):
    import sqlite3
    import time
    now = int(time.time() * 1000)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO enroll_requests (install_uuid, origin, suggested_title, "
            "protocol_version, secret_hash, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (install_uuid, origin, title, proto, secret_hash, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _login(client, token=ADMIN_TOKEN):
    """Log in over the JSON path; the session cookie lands in the client jar. Returns the
    raw Set-Cookie header so a test can assert its flags / extract the id."""
    r = client.post("/admin/login", json={"token": token})
    return r


def _session_value(client):
    return client.cookies.get(admin_session.COOKIE_NAME)


# --- (1) unauthenticated GET /admin -----------------------------------------
def test_admin_page_requires_auth_and_leaks_nothing(tmp_path):
    """Acc 1. Reddens if GET /admin stops calling require_admin (an anonymous hit would then
    receive the console HTML), or if the token appears in the 401 body."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        r = client.get("/admin")
        assert r.status_code == 401
        assert ADMIN_TOKEN not in r.text  # no secret leak in the unauthenticated body


# --- (2) login mints a hardened cookie whose value is NOT the token ----------
def test_login_sets_hardened_cookie_not_the_token(tmp_path):
    """Acc 2. Reddens if the cookie value ever becomes the ADMIN_TOKEN, or if any of
    HttpOnly / Secure / SameSite=Strict / Path=/admin is dropped."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        r = _login(client)
        assert r.status_code == 200
        set_cookie = r.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "secure" in set_cookie
        assert "samesite=strict" in set_cookie
        assert "path=/admin" in set_cookie
        value = _session_value(client)
        assert value and value != ADMIN_TOKEN  # the id is random, never the token
        # And the cookie actually opens the console.
        assert client.get("/admin").status_code == 200


# --- (3) CSRF: cookie mutation needs same-origin; Bearer skips it -------------
def test_csrf_cookie_mutation_blocked_cross_origin_bearer_allowed(tmp_path):
    """Acc 3. Reddens if require_admin drops the CSRF gate for cookie auth (the cross-origin
    cookie POST would reach the handler → 200/404 instead of 403), or if it wrongly applies
    CSRF to Bearer (the Bearer cross-origin POST would 403)."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with _tc(app) as client:
        _seed_request(db_path, "u-csrf", secret_hash=secret_hash_for("csrf"))
        _login(client)

        foreign = {"Origin": "https://evil.example.com"}
        # (a) cookie + FOREIGN Origin -> 403 even though the request is otherwise valid.
        r = client.post("/admin/enroll/reject", json={"install_uuid": "u-csrf"},
                        headers=foreign)
        assert r.status_code == 403

        # (b) cookie + Sec-Fetch-Site:same-origin (foreign Origin present) -> allowed:
        # the unforgeable fetch-metadata header takes precedence over the Origin fallback.
        r = client.post("/admin/enroll/reject", json={"install_uuid": "u-csrf"},
                        headers={**foreign, "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200

        # (c) Bearer + FOREIGN Origin, NO cookie -> Bearer skips CSRF; reaches handler.
        r = client.post("/admin/enroll/reject", json={"install_uuid": "u-none"},
                        headers={**admin_headers(), **foreign},
                        cookies={})
        assert r.status_code != 403
        assert r.status_code == 200  # reject is idempotent -> reaches the handler


def test_csrf_same_site_neighbor_refused(tmp_path):
    """Acc 3 (the shared-Traefik threat): a SAME-SITE (not same-origin) neighbor is refused.
    Reddens if the gate accepts anything other than exactly 'same-origin'."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        _login(client)
        r = client.post("/admin/enroll/window",
                        headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403


# --- (4) rotating ADMIN_TOKEN invalidates existing sessions ------------------
def test_token_rotation_invalidates_cookie(tmp_path):
    """Acc 4. Reddens if validate() stops comparing the stored fingerprint against the
    current sha256(admin_token) — an old cookie would keep working after rotation."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        _login(client)
        assert client.get("/admin").status_code == 200
        # Rotate the ADMIN_TOKEN in place; the stored session fingerprint no longer matches.
        client.app.state.settings.admin_token = "rotated-admin-token"
        assert client.get("/admin").status_code == 401


# --- (5) logout drops the session server-side --------------------------------
def test_logout_revokes_session_server_side(tmp_path):
    """Acc 5. Reddens if logout only clears the client cookie but leaves the id live in the
    store (a replayed cookie would still open /admin)."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        _login(client)
        sid = _session_value(client)
        assert client.get("/admin").status_code == 200
        # Same-origin signal: logout is a cookie-path mutating verb behind the CSRF gate.
        assert client.post(
            "/admin/logout", headers={"Sec-Fetch-Site": "same-origin"}
        ).status_code == 200
        # Replay the captured id explicitly: it must be gone from the SERVER store, not just
        # the client jar.
        r = client.get("/admin", headers={"Cookie": f"{admin_session.COOKIE_NAME}={sid}"})
        assert r.status_code == 401


# --- (6) XSS payload rendered as text, not markup ----------------------------
def test_xss_payload_served_verbatim_and_page_uses_textcontent(tmp_path):
    """Acc 6. Reddens if the JSON API HTML-encodes/strips the payload (breaking the text
    contract), or if the page JS is switched to innerHTML for untrusted fields."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    payload = "<img src=x onerror=alert(1)>"
    with _tc(app) as client:
        _seed_request(db_path, "u-xss", secret_hash=secret_hash_for("xss"), title=payload)
        rows = client.get("/admin/enroll/requests", headers=admin_headers()).json()["requests"]
        assert any(r["suggested_title"] == payload for r in rows)  # verbatim, not encoded
        # The rendering contract lives in the SHIPPED page JS: textContent, never innerHTML.
        served = client.get("/admin/app.js").text
        assert "textContent" in served
        assert "innerHTML" not in served


# --- (7) Bearer still opens everything, and beats an ambient cookie ----------
def test_bearer_opens_admin_and_beats_cookie(tmp_path):
    """Acc 7. Reddens if the #36 changes break the #35 Bearer path, or if an ambient cookie
    downgrades a Bearer call into the CSRF-gated cookie branch."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        # Bearer with NO cookie opens the console page and every JSON read.
        assert client.get("/admin", headers=admin_headers(), cookies={}).status_code == 200
        for path in ("/admin/enroll/requests", "/admin/instances", "/admin/enroll/window"):
            assert client.get(path, headers=admin_headers(), cookies={}).status_code == 200

        # Now a cookie ALSO exists; a Bearer request with a foreign Origin must still take the
        # Bearer path (no CSRF) — precedence over the ambient cookie.
        _login(client)
        r = client.post("/admin/enroll/window",
                        headers={**admin_headers(), "Origin": "https://evil.example.com"})
        assert r.status_code == 200


# --- (8) degraded: reads serve, writes 503 -----------------------------------
def test_degraded_reads_serve_writes_503(tmp_path):
    """Acc 8. Reddens if GET /admin starts calling require_operational (the console would go
    dark in degraded mode) or if a mutating verb drops it."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        client.app.state.degraded = True
        assert client.get("/admin", headers=admin_headers()).status_code == 200
        assert client.get("/admin/instances", headers=admin_headers()).status_code == 200
        r = client.post("/admin/enroll/approve", headers=admin_headers(),
                        json={"install_uuid": "u", "instance_id": "i"})
        assert r.status_code == 503


# --- session expiry (server-side TTL) ----------------------------------------
def test_session_expiry_unit(tmp_path):
    """Past-TTL session is invalid. Reddens if validate() stops checking expires_at."""
    app = create_app_for(tmp_path)
    with TestClient(app):
        t0 = 1_000_000
        sid = admin_session.create(app, now=t0)
        ttl_ms = app.state.settings.admin_session_ttl_min * 60_000
        assert admin_session.validate(app, sid, now=t0 + ttl_ms - 1) is True
        assert admin_session.validate(app, sid, now=t0 + ttl_ms) is False  # boundary: expired
        # And it was evicted on the failed touch (drop-on-touch), so it stays invalid.
        assert admin_session.validate(app, sid, now=t0) is False


def test_session_expiry_through_request(tmp_path):
    """An expired session no longer opens /admin through the real request path. Reddens if
    require_admin trusts a stale cookie."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        _login(client)
        sid = _session_value(client)
        assert client.get("/admin").status_code == 200
        # Force the stored entry into the past.
        expires_at, fp = client.app.state.admin_sessions[sid]
        client.app.state.admin_sessions[sid] = (0, fp)
        assert client.get("/admin").status_code == 401


# --- wrong-token login -------------------------------------------------------
def test_wrong_token_login_rejected_no_cookie(tmp_path):
    """Login with a wrong token → 401 and NO Set-Cookie. Reddens if login stops comparing
    the token (any string would mint a session)."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        r = client.post("/admin/login", json={"token": "not-the-admin-token"})
        assert r.status_code == 401
        assert "set-cookie" not in {k.lower() for k in r.headers}
        assert _session_value(client) is None


# --- framing defenses + nosniff (review round) -------------------------------
def test_admin_html_and_assets_carry_frame_defenses(tmp_path):
    """The authenticated console page AND its assets carry the CSP (incl. frame-ancestors
    'none'), X-Frame-Options: DENY and nosniff. Reddens if the clickjacking defenses are
    dropped — a same-site neighbor could then iframe the console and clickjack a same-origin
    (CSRF-passing) mutation. Also pins that authenticated admin.html carries the CSP at all."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        # Authenticated GET /admin (Bearer) — the HTML the operator actually loads.
        page = client.get("/admin", headers=admin_headers())
        assert page.status_code == 200
        for resp in (page,
                     client.get("/admin/app.js"),
                     client.get("/admin/app.css"),
                     client.get("/admin/login"),
                     client.get("/admin/login.js")):
            csp = resp.headers["content-security-policy"]
            assert "default-src 'none'" in csp
            assert "frame-ancestors 'none'" in csp
            assert resp.headers["x-frame-options"] == "DENY"
            assert resp.headers["x-content-type-options"] == "nosniff"


def test_admin_json_carries_nosniff(tmp_path):
    """Every /admin JSON response carries X-Content-Type-Options: nosniff (untrusted
    suggested_title/origin are served verbatim). Reddens if the header middleware stops
    covering the JSON API — without changing the JSON body."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        r = client.get("/admin/instances", headers=admin_headers())
        assert r.status_code == 200
        assert r.headers["x-content-type-options"] == "nosniff"
        # Body/behavior of the #35 JSON is unchanged.
        assert "instances" in r.json()


def test_logout_requires_same_origin(tmp_path):
    """A cross-/same-site forced POST /admin/logout is refused (403) and the session still
    opens the console. Reddens if logout drops the CSRF gate (a neighbor could force-logout
    the operator with the ambient cookie)."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        _login(client)
        # same-site (not same-origin) neighbor forcing a logout -> refused.
        r = client.post("/admin/logout", headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403
        # The session survived the refused logout.
        assert client.get("/admin").status_code == 200


def test_expired_session_swept_on_create(tmp_path):
    """An expired, NEVER-re-presented session id is physically removed from the store on the
    next create() (bounded store). Reddens if create() stops sweeping — the id would linger
    forever."""
    app = create_app_for(tmp_path)
    with _tc(app):
        t0 = 1_000_000
        stale = admin_session.create(app, now=t0)
        # Force it expired without ever validating (re-presenting) it.
        _exp, fp = app.state.admin_sessions[stale]
        app.state.admin_sessions[stale] = (t0, fp)  # expires_at in the past
        # A fresh login well after t0 must sweep the stale id out.
        fresh = admin_session.create(app, now=t0 + 10_000)
        assert stale not in app.state.admin_sessions
        assert fresh in app.state.admin_sessions


# --- assets are public + carry CSP -------------------------------------------
def test_assets_public_with_csp(tmp_path):
    """The login form + .js/.css assets are served WITHOUT auth and carry the explicit CSP.
    Reddens if an asset is put behind the guard, or the CSP header is dropped."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        for path, needle in (
            ("/admin/login", "text/html"),
            ("/admin/app.js", "javascript"),
            ("/admin/app.css", "text/css"),
            ("/admin/login.js", "javascript"),
        ):
            r = client.get(path)  # no auth
            assert r.status_code == 200, path
            assert needle in r.headers["content-type"]
            csp = r.headers["content-security-policy"]
            assert "default-src 'none'" in csp
            assert "script-src 'self'" in csp
