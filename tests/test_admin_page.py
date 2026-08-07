"""``/admin`` HTML console (issue #36) — the cookie-session + CSP + CSRF layer over the
#35 JSON API. Each test maps a numbered acceptance row and is written to REDDEN on the
specific invariant it guards (noted inline).

* (1) GET /admin with no cookie AND no Bearer → the console is NOT served; the human is
      sent to /admin/login (303) with no ADMIN_TOKEN in the body. The JSON API under
      /admin/* keeps answering a flat 401 — the split is per surface, not a weaker gate.
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
* the page and its JS keep their end of the CSP bargain: no inline style/script survives in
  the templates, and every element the JS binds to exists in the HTML.
"""

import re

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


# --- low-level seed of an instances row --------------------------------------
# The mutating verb these tests exercise is `revoke`; there is no approve/reject pair
# anymore (§6), so an instance is what has to exist for a mutation to land on.
def _seed_instance(db_path, iid, *, status="active", secret_hash=None):
    import sqlite3
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


def _login(client, token=ADMIN_TOKEN):
    """Log in over the JSON path; the session cookie lands in the client jar. Returns the
    raw Set-Cookie header so a test can assert its flags / extract the id."""
    r = client.post("/admin/login", json={"token": token})
    return r


def _session_value(client):
    return client.cookies.get(admin_session.COOKIE_NAME)


# A fragment of templates/admin.html that appears NOWHERE else (not in login.html): if it
# is in a response, the console body was served.
_CONSOLE_MARKER = 'id="instances-body"'


def _console_refused(client, **kw):
    """Assert GET /admin refused the caller as a BROWSER: no console body, and a hop to the
    login form. Returns the response.

    ``follow_redirects=False`` is load-bearing — the TestClient follows redirects by
    default, so without it the hop under test is invisible (one would see the login page
    at 200 and could not tell it from the console being served).
    """
    r = client.get("/admin", follow_redirects=False, **kw)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    assert _CONSOLE_MARKER not in r.text
    return r


# --- (1) unauthenticated GET /admin -----------------------------------------
def test_root_redirects_to_the_console(tmp_path):
    """`/` must open SOMETHING. A bare "Not Found" on the service address reads as
    "the service is down", and forces the operator to know that the console lives at
    /admin and that a logged-out visit needs /admin/login. Nothing should require that
    knowledge. One hop to /admin covers both cases — /admin itself forwards a
    logged-out browser to the login form."""
    app = create_app_for(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/admin"
        # And the hop actually lands somewhere useful for a logged-out browser.
        landed = client.get("/", follow_redirects=True)
        assert landed.status_code == 200
        assert "/admin/login" in str(landed.url)


def test_admin_page_refuses_console_and_points_at_the_login_form(tmp_path):
    """Acc 1, amended: the PAGE refuses by sending the human to the login form.

    Both halves matter and fail differently. The gate half — reddens if GET /admin stops
    calling require_admin (an anonymous hit would then receive the console HTML, which the
    marker assertion catches even though the status would be 200). The usability half —
    reddens if the refusal goes back to a bare 401, which is what an operator typing
    https://…/admin into the address bar used to get: the plain text "admin authentication
    required", with the login form one public URL away and reachable only from the console
    JS, i.e. only for a console that had already loaded.
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        r = _console_refused(client)
        assert ADMIN_TOKEN not in r.text  # no secret leak in the unauthenticated body

        # And the hop terminates: the target is public, so it answers the form itself
        # rather than refusing again (a guarded target would make this a loop).
        form = client.get(r.headers["location"])
        assert form.status_code == 200
        assert 'id="login-form"' in form.text
        assert _CONSOLE_MARKER not in form.text


def test_admin_json_api_still_401s_and_never_redirects(tmp_path):
    """The redirect is confined to the HTML page; the JSON API keeps its flat 401.

    A redirect is a fine answer for a person and a bad one for a program: ``fetch`` follows
    by default, so a console whose session expired mid-session would receive the login HTML
    where it expected its payload and would have to guess. Reddens if the redirect is moved
    into ``require_admin`` (every /admin/* JSON route would inherit it), and — via the
    Bearer case — if a rejected CREDENTIAL starts being answered with a page: that also
    keeps #35 acc 6, an instance secret probing /admin getting the same 401 as an unknown
    token, with no page to tell them apart by.
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        for path in ("/admin/instances", "/admin/enroll/window"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 401, path
            assert "location" not in {k.lower() for k in r.headers}, path

        # A PRESENTED-but-wrong Bearer on the page route is a program, not a browser: 401.
        r = client.get("/admin", follow_redirects=False,
                       headers={"Authorization": "Bearer not-the-admin-token"})
        assert r.status_code == 401
        assert "location" not in {k.lower() for k in r.headers}
        assert ADMIN_TOKEN not in r.text


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
        _seed_instance(db_path, "victim", secret_hash=secret_hash_for("victim"))
        _login(client)

        foreign = {"Origin": "https://evil.example.com"}
        # (a) cookie + FOREIGN Origin -> 403 even though the request is otherwise valid.
        r = client.post("/admin/instances/victim/revoke", json={}, headers=foreign)
        assert r.status_code == 403

        # (b) cookie + Sec-Fetch-Site:same-origin (foreign Origin present) -> allowed:
        # the unforgeable fetch-metadata header takes precedence over the Origin fallback.
        r = client.post("/admin/instances/victim/revoke", json={},
                        headers={**foreign, "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200

        # (c) Bearer + FOREIGN Origin, NO cookie -> Bearer skips CSRF; reaches handler.
        # 404 (no such instance) is the proof it REACHED the handler rather than the gate.
        r = client.post("/admin/instances/ghost/revoke", json={},
                        headers={**admin_headers(), **foreign},
                        cookies={})
        assert r.status_code != 403
        assert r.status_code == 404


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
        _console_refused(client)  # dead cookie -> no console, back to the login form


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
        # the client jar. The console body is what must not come back — the status is the
        # browser-facing hop to the login form.
        _console_refused(client, headers={"Cookie": f"{admin_session.COOKIE_NAME}={sid}"})


# --- (6) the console renders as text, not markup -----------------------------
def test_page_uses_textcontent_and_no_unbounded_client_string_reaches_it(tmp_path):
    """Acc 6, in the shape the console has now.

    The payload half of this test is gone WITH ITS SOURCE. The console used to print two
    client-supplied strings verbatim — ``suggested_title`` and ``origin`` off a pending
    enroll_request — so an unauthenticated peer could put ``<img src=x onerror=…>`` in
    front of the operator, and the whole defence was the page's textContent discipline.
    There is no pending list anymore (§6) and no free-text field behind it: the only
    client-influenced value the console prints is the instance id, which the service
    refuses unless it matches ``[A-Za-z0-9._-]{1,64}`` before the row is ever created.

    The textContent contract is still pinned, because it is what keeps the NEXT field from
    being a hole. Reddens if the page JS is switched to innerHTML anywhere.
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        served = client.get("/admin/app.js").text
        assert "textContent" in served
        assert "innerHTML" not in served


# --- the operator must SEE the refusal reason, not just its status code -------
def test_error_detail_reaches_the_console_not_just_the_status(tmp_path):
    """A refusal carries a WRITTEN reason and the console must be able to print it.

    ``_http_exception`` (src/app.py) renders a dict ``detail`` as JSON and every other
    ``detail`` as PLAIN TEXT. The console's ``apiSend`` used to read only
    ``res.json().error``, so every string detail was lost to the failed parse and the
    operator saw a bare "-> 409" — while the MAIN-revoke refusal spells out both what
    revoking MAIN does and what it does NOT do (it cannot hand MAIN to another instance).

    ``apiGet`` had the same hole for longer: it threw ``path + " -> " + res.status`` and
    every ``render*`` call goes through it, so a degraded service showed
    "/admin/instances -> 503" instead of the sentence the server sent. The extraction
    therefore lives in ONE helper both wrappers call — the thing this test pins, because a
    second copy is exactly how the two paths drifted apart the first time.

    There is no JS harness for ``templates/app.js`` (it is a served static asset, not a
    module — see the acc-6 test above for the same source-level style), so this pins the
    two halves that must meet: the server really does put the sentence in a plain-text
    body, and the SHIPPED page JS reads that body as text. Reddens if the detail is
    swallowed on either side — in particular if the helper goes back to ``res.json()``
    first, which consumes the body and makes any ``res.text()`` fallback unreachable, or
    if either wrapper stops routing its refusal through the helper."""
    app = create_app_for(tmp_path)
    db_path = str(tmp_path / "curator.db")
    with _tc(app) as client:
        _seed_instance(db_path, "main", secret_hash=secret_hash_for("main"))
        # Revoking MAIN without the repeated id → the 409 whose detail names the guard.
        r = client.post("/admin/instances/main/revoke", headers=admin_headers(), json={})
        assert r.status_code == 409
        assert "text/plain" in r.headers["content-type"]  # NOT JSON: no `error` field
        assert "replacement" in r.text
        assert "MAIN_INSTANCE_ID" in r.text

        # The shipped console reads the body as TEXT and only then tries to parse it as
        # JSON — the order matters, res.json() consumes the body on a failed parse.
        served = client.get("/admin/app.js").text

        def _source_of(signature):
            """The body of one TOP-LEVEL function: from its signature to the first closing
            brace in column 0. Excludes the comment block above it, which names res.json()
            precisely to explain why the code does not call it."""
            src = served[served.index(signature):]
            return src[: src.index("\n}\n")]

        # Comments inside the helper NAME res.json() for the same reason; strip them so the
        # assertions below read the CODE.
        code = "\n".join(
            line for line in _source_of("async function responseError(").splitlines()
            if not line.strip().startswith("//")
        )
        assert "res.text()" in code
        assert "JSON.parse(" in code
        assert code.index("res.text()") < code.index("JSON.parse(")
        assert "res.json()" not in code  # would consume the body before the text read
        assert "err.status = res.status" in code  # the MAIN-revoke 409 branch reads it

        # BOTH wrappers report a refusal through that ONE helper, and neither keeps a
        # private copy of the extraction (a second copy is how apiGet drifted).
        for signature in ("async function apiGet(", "async function apiSend("):
            wrapper = _source_of(signature)
            assert "responseError(" in wrapper, signature
            assert "res.text()" not in wrapper, signature
            assert "JSON.parse(" not in wrapper, signature


# --- the page and its JS have to agree about the DOM -------------------------
def _strip_html_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _strip_js_line_comments(text):
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("//")
    )


def test_every_element_the_console_js_binds_to_exists_in_the_page(tmp_path):
    """Every id ``app.js`` looks up is really in ``admin.html``.

    The console builds its whole surface by id: one missing id is a ``getElementById``
    returning ``null`` and a TypeError the next line, and because the page logic runs from a
    separately-served asset, NOTHING on the server side would notice — the page would render
    its shell and simply stay empty. There is no JS harness for these templates (they are
    served static assets, not modules), so the two files are checked against each other at
    the source level, which is the only place the mismatch is visible.

    The second half pins the ids that are load-bearing by contract rather than by accident:
    the build stamp, the session control, the page-wide error slot, and the sections the
    console is made of. Reddens if one is renamed on one side only — or dropped from the
    page while the JS still reaches for it.
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        html = client.get("/admin", headers=admin_headers()).text
        js = client.get("/admin/app.js").text

    # `byId` is app.js's own one-line wrapper over getElementById; both spellings count.
    wanted = set(re.findall(r'(?:getElementById|byId)\(\s*"([^"]+)"\s*\)', js))
    present = set(re.findall(r'id="([^"]+)"', html))
    assert wanted, "no id lookups found in app.js — the pattern stopped matching"
    assert wanted <= present, f"app.js binds to ids the page lacks: {sorted(wanted - present)}"
    assert {
        "build-revision", "logout", "error",
        "window-section", "window-status", "open-window", "close-window",
        "instances-section", "instances-body", "instances-empty",
    } <= present


def test_console_assets_carry_no_inline_style_or_script(tmp_path):
    """The CSP is ``default-src 'none'; script-src 'self'; style-src 'self'`` — so an inline
    ``<style>``, an inline ``<script>`` or a ``style=`` attribute is not "slightly wrong",
    it is DEAD: the browser drops it and the page silently loses whatever it expressed.

    The trap this guards is the countdown bar. Drawing a drain bar wants a width, and a
    width wants ``el.style.width`` or a ``style=`` attribute — both refused under this
    policy, and refused SILENTLY as far as the server is concerned. The page therefore
    states progress with a ``<progress>`` element, whose ``value``/``max`` are content
    attributes. Reddens if any template grows an inline block or attribute, or if app.js
    starts writing styles (comments are stripped first, so the policy may still be
    discussed in prose).
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        for path in ("/admin", "/admin/login"):
            body = _strip_html_comments(
                client.get(path, headers=admin_headers()).text
            )
            assert "<style" not in body, path
            assert "<script>" not in body, path  # <script src=…> is the allowed shape
            assert not re.search(r"<[^>]*\sstyle\s*=", body), path
        for path in ("/admin/app.js", "/admin/login.js"):
            code = _strip_js_line_comments(client.get(path).text)
            assert ".style" not in code, path
            assert "setAttribute" not in code, path


def test_the_countdown_timer_is_cleared_and_never_doubled(tmp_path):
    """The registration countdown ticks client-side, so it owns an interval — and an
    interval that is started without being cleared runs forever behind a card that is no
    longer on screen, redrawing a countdown for a window that closed.

    There is exactly ONE place that arms it, and the code clears before it arms. Reddens if
    a second ``setInterval`` appears (two tickers racing on the same element) or if
    ``clearInterval`` is dropped.
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        code = _strip_js_line_comments(client.get("/admin/app.js").text)
    assert code.count("setInterval(") == 1
    assert "clearInterval(" in code


# --- (7) Bearer still opens everything, and beats an ambient cookie ----------
def test_bearer_opens_admin_and_beats_cookie(tmp_path):
    """Acc 7. Reddens if the #36 changes break the #35 Bearer path, or if an ambient cookie
    downgrades a Bearer call into the CSRF-gated cookie branch."""
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        # Bearer with NO cookie opens the console page and every JSON read.
        assert client.get("/admin", headers=admin_headers(), cookies={}).status_code == 200
        for path in ("/admin/instances", "/admin/enroll/window"):
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
        r = client.post("/admin/instances/anything/revoke", headers=admin_headers(), json={})
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
        _console_refused(client)  # stale cookie -> no console, back to the login form


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


def test_failed_login_counts_under_its_own_reason(tmp_path):
    """A wrong ADMIN_TOKEN must be countable APART from "no session presented".

    ``admin_session`` ticks on every unauthenticated hit of the console — a browser opening
    /admin before logging in produces one — so folding failed logins into it makes guessing
    the credential that opens /admin, /api/* and /mcp unalertable: no threshold survives
    the background noise and still catches a brute force. Reddens if login_submit goes back
    to incrementing ``admin_session`` (the bad-token counter would stop moving and the
    ambient one would move twice).

    Half (b) is also what keeps the login redirect honest. Sending the anonymous page hit
    to the login form did not make it a non-event: it is still a refusal and still counts,
    under the AMBIENT label that carries no alert rule. Reddens in the other direction too
    — if the redirect were ever counted as ``admin_bad_token``, every operator opening the
    console cold would push the brute-force rule toward firing on nothing.
    """
    from src.api.admin_page import ADMIN_BAD_TOKEN_REASON
    from src.api.auth_metrics import auth_rejections

    app = create_app_for(tmp_path)
    with _tc(app) as client:
        before = auth_rejections.by_reason()
        # (a) a wrong token at the LOGIN endpoint -> the brute-force reason.
        assert client.post("/admin/login", json={"token": "guess"}).status_code == 401
        mid = auth_rejections.by_reason()
        assert mid.get(ADMIN_BAD_TOKEN_REASON, 0) == before.get(ADMIN_BAD_TOKEN_REASON, 0) + 1
        assert mid.get("admin_session", 0) == before.get("admin_session", 0)

        # (b) an ordinary unauthenticated console hit -> the ambient reason, and NOT the
        # brute-force one (this is the noise the split exists to keep out of the alert).
        _console_refused(client, cookies={})
        after = auth_rejections.by_reason()
        assert after.get("admin_session", 0) == mid.get("admin_session", 0) + 1
        assert after.get(ADMIN_BAD_TOKEN_REASON, 0) == mid.get(ADMIN_BAD_TOKEN_REASON, 0)


# --- pre-auth body ceiling ---------------------------------------------------
def test_login_body_is_capped(tmp_path):
    """``POST /admin/login`` is the service's only PRE-AUTH body, and it was unbounded.

    It cannot sit behind ``require_admin`` (it is how one authenticates) and neither the
    app nor uvicorn imposes a size limit, so ``await request.json()`` would buffer whatever
    an anonymous peer sent — the cheapest possible way to spend the process's memory.
    Reddens if the cap is removed: the 1 MiB post below turns back into a 401 (i.e. it was
    read in full first).
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        # (a) declared oversize: refused on the header, before a byte is buffered.
        big = client.post(
            "/admin/login",
            content=b'{"token": "' + b"x" * (1024 * 1024) + b'"}',
            headers={"Content-Type": "application/json"},
        )
        assert big.status_code == 413

        # (b) CHUNKED (no Content-Length — the trivial way past a header check) is cut off
        # at the same ceiling by the running total.
        def _chunks():
            yield b'{"token": "'
            for _ in range(64):
                yield b"x" * 1024
            yield b'"}'

        chunked = client.post(
            "/admin/login", content=_chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert chunked.status_code == 413

        # (c) a normal login is unaffected — the cap is orders of magnitude above a token.
        assert client.post("/admin/login", json={"token": ADMIN_TOKEN}).status_code == 200


def test_bounded_body_binds_every_later_reader():
    """The ceiling must hold for whoever reads the body NEXT, not just for this call.

    ``read_bounded_body`` stashes its buffer in Starlette's PRIVATE ``Request._body`` —
    the same attribute ``Request.body()`` reads and writes — because Starlette exposes no
    public equivalent (no setter, no "already read" hook). That coupling is the thing an
    upgrade can break SILENTLY: the guard would keep returning 413s while every later
    reader went back to the wire, and no existing test would notice. So both directions are
    pinned here, against the real ASGI stack rather than a hand-built Request:

    * accepted body -> ``request.body()`` returns THAT buffer (if the stash stopped being
      honoured, this raises "Stream consumed" instead — loud, not silent);
    * refused body -> a later read yields nothing. The declared-Content-Length branch is
      the sharp one: it returns before touching the stream, so without a sticky refusal the
      next ``request.body()`` happily buffered the whole oversized body (measured: 1000
      bytes past a 64-byte limit) and the ceiling bounded nothing at all.
    """
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from src.api.guards import read_bounded_body

    limit = 64

    async def probe(request):
        out = {}
        try:
            out["first"] = len(await read_bounded_body(request, limit))
        except HTTPException as exc:
            out["first"] = exc.status_code
        # Whatever happened above, this is what the NEXT reader of the body sees.
        out["second"] = len(await request.body())
        return JSONResponse(out)

    probe_app = Starlette(routes=[Route("/probe", probe, methods=["POST"])])
    with TestClient(probe_app) as client:
        # Accepted: the stash is what Starlette's own reader returns.
        assert client.post("/probe", content=b"z" * 10).json() == {"first": 10, "second": 10}

        # Refused on the declared Content-Length — nothing was read, and nothing may be.
        assert client.post("/probe", content=b"y" * 1000).json() == {"first": 413, "second": 0}

        # Refused mid-stream on a CHUNKED body (no Content-Length): same guarantee.
        def _chunks():
            for _ in range(10):
                yield b"x" * 100

        assert client.post("/probe", content=_chunks()).json() == {"first": 413, "second": 0}


def test_a_refusal_does_not_destroy_a_body_someone_else_already_read():
    """The already-buffered branch RE-CHECKS a buffer; it must never wipe it.

    Sticky-on-refusal exists for the branches that leave the wire readable — it stops a
    later reader from going back and buffering the body this call refused. On the branch
    where the body is already in ``Request._body`` there is no wire left to protect (the
    stream is drained), and the buffer belongs to whoever read it first: pinning it to
    ``b""`` there deleted a valid body before anyone had even decided whether to catch the
    413. Harmless only for as long as the single caller keeps letting the exception fly.

    Both outcomes of that branch are pinned, on the real ASGI stack rather than a hand-built
    Request, because they fail differently:

    * a LEGAL pre-read body (within the limit) is handed back BYTE-FOR-BYTE and is still
      there for the next reader — an unconditional wipe on this branch would be invisible
      to the 413 case alone;
    * an oversized one still answers 413, and the buffer survives it — reddens the moment
      the wipe is put back on this branch, ``second`` dropping to 0.

    Both bodies are sent CHUNKED (no ``Content-Length``) so the header check cannot short-
    circuit them and the already-buffered branch is really the one under test.
    """
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from src.api.guards import read_bounded_body

    limit = 64

    async def probe(request):
        # An earlier reader legitimately buffers the whole body first.
        first = await request.body()
        out = {"first": len(first), "verbatim": None}
        try:
            bounded = await read_bounded_body(request, limit)
            out["bounded"] = len(bounded)
            out["verbatim"] = bounded == first  # returned, not merely sized right
        except HTTPException as exc:
            out["bounded"] = exc.status_code
        # Whatever happened above, this is what the NEXT reader of the body sees.
        out["second"] = len(await request.body())
        return JSONResponse(out)

    probe_app = Starlette(routes=[Route("/probe", probe, methods=["POST"])])

    def _chunks(count):
        def gen():
            for _ in range(count):
                yield b"x" * 10
        return gen()

    with TestClient(probe_app) as client:
        # (a) within the limit: the earlier reader's buffer comes back untouched.
        assert client.post("/probe", content=_chunks(3)).json() == {
            "first": 30,
            "bounded": 30,
            "verbatim": True,
            "second": 30,
        }

        # (b) over the limit on the SAME branch: 413, and the buffer is still whole.
        assert client.post("/probe", content=_chunks(100)).json() == {
            "first": 1000,
            "bounded": 413,
            "verbatim": None,
            "second": 1000,
        }


# --- no-store on every /admin response ---------------------------------------
def test_admin_responses_are_never_cached(tmp_path):
    """Every ``/admin`` response carries ``Cache-Control: no-store``.

    ``GET /admin/enroll/window`` returns the LIVE window code — the credential an operator
    reads out loud to enrol a browser — and with no cache directive at all the default
    heuristics let a browser or intermediary write it to the DISK cache, where it outlives
    both the window and the session. ``no-store`` is the only directive that forbids
    writing it down (``no-cache`` still allows a stored copy). Reddens if the middleware
    stops stamping it, or stamps the weaker directive.
    """
    app = create_app_for(tmp_path)
    with _tc(app) as client:
        window = client.get("/admin/enroll/window", headers=admin_headers())
        assert window.status_code == 200
        assert window.headers["cache-control"] == "no-store"
        # …and the same on the page, the assets, the login form and the JSON reads —
        # including the unauthenticated page's 303, which is a response on /admin like any
        # other and must not be remembered by anything.
        for resp in (
            client.get("/admin", headers=admin_headers()),
            client.get("/admin", follow_redirects=False),
            client.get("/admin/instances", headers=admin_headers()),
            client.get("/admin/login"),
            client.get("/admin/app.js"),
            client.get("/admin/app.css"),
        ):
            assert resp.headers["cache-control"] == "no-store"


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
    """Every /admin JSON response carries X-Content-Type-Options: nosniff. Reddens if the
    header middleware stops covering the JSON API — without changing the JSON body. The
    bodies no longer carry free-text client input, but the header is what keeps the next
    field added to them from being a MIME-sniffing hole."""
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


# --- build revision on the console page --------------------------------------
def test_console_shows_the_running_build_revision(tmp_path):
    """The authenticated console states which revision it is running, IN THE HTML.

    Server-side substitution, not a `fetch` from app.js: the page must answer "is this the
    new code?" even when every JSON call behind it is failing — which is exactly when the
    question gets asked. Reddens if the placeholder stops being replaced (a literal
    `__BUILD_REVISION__` on screen) or if the value is dropped.
    """
    app = create_app_for(tmp_path, build_revision="0f2c9a1b3d4e5f6071")
    with _tc(app) as client:
        r = client.get("/admin", headers=admin_headers())
        assert r.status_code == 200
        assert "0f2c9a1b3d4e5f6071" in r.text
        assert "__BUILD_REVISION__" not in r.text


def test_console_says_unknown_for_an_unstamped_build(tmp_path):
    """A build with no revision renders the word, never an empty element — the same
    "unknown" /healthz reports. An empty slot reads as a broken page, not as a missing
    stamp."""
    app = create_app_for(tmp_path, build_revision="unknown")
    with _tc(app) as client:
        r = client.get("/admin", headers=admin_headers())
        assert r.status_code == 200
        assert "unknown" in r.text
        assert "__BUILD_REVISION__" not in r.text


def test_console_escapes_the_revision_it_renders(tmp_path):
    """The revision is interpolated into HTML, so it is escaped where it is interpolated.

    The value comes from an environment variable stamped by OUR image build, so this is not
    a live threat — it is the guard that keeps it from becoming one if that ever stops being
    true (a hand-built image, an operator who set BUILD_REVISION by hand). Reddens if the
    substitution is switched to a raw splice.
    """
    app = create_app_for(tmp_path, build_revision="<script>alert(1)</script>")
    with _tc(app) as client:
        r = client.get("/admin", headers=admin_headers())
        assert r.status_code == 200
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_the_revision_is_not_leaked_on_the_public_login_page(tmp_path):
    """GET /admin/login is PUBLIC; the console page is not. The revision belongs on the
    gated page (and on /healthz, which is a deliberate, documented disclosure) — it must not
    quietly appear on the login form as a side effect of touching the template pipeline."""
    app = create_app_for(tmp_path, build_revision="0f2c9a1b3d4e5f6071")
    with _tc(app) as client:
        r = client.get("/admin/login")
        assert r.status_code == 200
        assert "0f2c9a1b3d4e5f6071" not in r.text
