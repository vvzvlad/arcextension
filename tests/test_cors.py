"""CORS on /api/* (§12): explicit chrome-extension:// origins only, NEVER '*'.

The startpage fetches /api/* cross-origin with an Authorization Bearer header, which
forces a CORS preflight. These tests pin the allow-list behaviour end-to-end through
the real ``create_app`` middleware stack (Starlette ``TestClient``), and the two
acceptance auth gates (/metrics needs METRICS_TOKEN, /api/* needs EXT_TOKEN).

Each test reddens if its guard is removed: dropping the CORSMiddleware drops the
Access-Control-Allow-Origin echo (test 1), and widening allow_origins to '*' or a
regex would make test 3 (evil origin) start receiving an ACAO.
"""

from types import SimpleNamespace

from conftest import make_settings
from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
METRICS_TOKEN = "test-metrics-token"
GOOD_ORIGIN = "chrome-extension://abc"
EVIL_ORIGIN = "https://evil.com"
OTHER_EXT_ORIGIN = "chrome-extension://zzz"  # a non-listed extension id
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
    return make_settings(tmp_path, **{**{
            "ext_allowed_origins": 'chrome-extension://abc',
            "pass_interval_min": 5,
        }, **over})


def _preflight(client, origin, path="/api/state", method="GET"):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


# --- allow-list: preflight ---------------------------------------------------
def test_preflight_allowed_origin_is_echoed(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = _preflight(client, GOOD_ORIGIN)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == GOOD_ORIGIN
        # Never '*', even on the happy path.
        assert r.headers["access-control-allow-origin"] != "*"


def test_preflight_evil_origin_gets_no_acao(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = _preflight(client, EVIL_ORIGIN)
        # Non-vacuity: the CORS middleware ACTIVELY rejects a disallowed preflight with
        # 400. Without the middleware, OPTIONS on a GET-only route is a plain 405 — so
        # this 400 proves the guard is present, not merely that no ACAO leaked.
        assert r.status_code == 400
        assert r.headers.get("access-control-allow-origin") != EVIL_ORIGIN
        assert r.headers.get("access-control-allow-origin") != "*"


def test_preflight_nonlisted_extension_id_gets_no_acao(tmp_path):
    # A DIFFERENT extension id (the §12 silent-failure case) must not be allowed.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = _preflight(client, OTHER_EXT_ORIGIN)
        assert r.status_code == 400  # actively rejected by the middleware, not a 405
        assert r.headers.get("access-control-allow-origin") != OTHER_EXT_ORIGIN
        assert r.headers.get("access-control-allow-origin") != "*"


# --- allow-list: simple (non-preflight) response -----------------------------
def test_simple_get_echoes_allowed_origin_never_star(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = client.get("/api/state", headers={**AUTH, "Origin": GOOD_ORIGIN})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == GOOD_ORIGIN
        assert r.headers["access-control-allow-origin"] != "*"


def test_simple_get_evil_origin_gets_no_acao(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = client.get("/api/state", headers={**AUTH, "Origin": EVIL_ORIGIN})
        # The request itself still succeeds (CORS is browser-enforced) but the
        # response carries no ACAO for the evil origin, so the browser blocks it.
        assert r.headers.get("access-control-allow-origin") != EVIL_ORIGIN
        assert r.headers.get("access-control-allow-origin") != "*"


# --- never '*' even with multiple explicit origins configured ----------------
def test_multiple_origins_never_wildcard(tmp_path):
    two = f"{GOOD_ORIGIN},{OTHER_EXT_ORIGIN}"
    app = create_app(_settings(tmp_path, ext_allowed_origins=two))
    with TestClient(app) as client:
        for origin in (GOOD_ORIGIN, OTHER_EXT_ORIGIN):
            r = _preflight(client, origin)
            assert r.status_code == 200
            assert r.headers["access-control-allow-origin"] == origin
            assert r.headers["access-control-allow-origin"] != "*"


# --- empty allow-list => cross-origin /api/* CLOSED, never widened to '*' -----
def test_empty_allowlist_blocks_cross_origin_never_star(tmp_path):
    app = create_app(_settings(tmp_path, ext_allowed_origins=""))
    with TestClient(app) as client:
        r = _preflight(client, GOOD_ORIGIN)
        # No origin is allowed (secure default), and it is NEVER turned into '*'.
        # 400 (not 405) proves the middleware is present and actively closing the door.
        assert r.status_code == 400
        assert r.headers.get("access-control-allow-origin") != GOOD_ORIGIN
        assert r.headers.get("access-control-allow-origin") != "*"


def test_literal_star_in_env_is_dropped_never_wildcard(tmp_path):
    # An operator typo EXT_ALLOWED_ORIGINS="*" must NOT widen /api/* to any origin:
    # cors_kwargs drops the literal '*', leaving the empty-list secure default.
    app = create_app(_settings(tmp_path, ext_allowed_origins="*"))
    with TestClient(app) as client:
        r = _preflight(client, GOOD_ORIGIN)
        assert r.status_code == 400  # '*' dropped => nothing allowed => rejected
        assert r.headers.get("access-control-allow-origin") not in (GOOD_ORIGIN, "*")


# --- acceptance auth gates (pinned here too) ---------------------------------
def test_metrics_requires_metrics_token(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        # EXT_TOKEN must NOT open /metrics (§12: separate read-only credential).
        assert client.get(
            "/metrics", headers={"Authorization": f"Bearer {EXT_TOKEN}"}
        ).status_code == 401
        assert client.get(
            "/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}
        ).status_code == 200


def test_api_requires_ext_token(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # read
        assert client.get("/api/state").status_code == 401
        # write
        assert client.post(
            "/api/focus", json={"instance": "i1", "tabId": 1}
        ).status_code == 401
        # METRICS_TOKEN must NOT open /api/* (only EXT_TOKEN does, §12).
        assert client.get(
            "/api/state", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}
        ).status_code == 401


# --- the empty-value asymmetry, pinned in ONE place --------------------------
def test_empty_allowlist_is_open_on_ext_and_closed_on_cors(tmp_path):
    """The two consumers of an EMPTY ``EXT_ALLOWED_ORIGINS`` read it differently on
    purpose, and that is the §12 «бесшумный отказ» shape: the websocket connects (the
    instance looks healthy everywhere) while the startpage's fetch dies on preflight.

    Closing ``/ext`` by default is the worse option — the concrete
    ``chrome-extension://<id>`` is unknowable before the extension is loaded, so it
    would make bootstrap impossible and would disconnect every instance of a deployment
    that never set the variable. CORS cannot widen to ``*`` (§12), so its empty case can
    only be "closed". This test pins BOTH halves so the asymmetry stays a decision
    rather than drifting, and the rejected preflight is counted (the audible signal).
    """
    from src.api.auth_metrics import auth_rejections
    from src.ext.protocol import hello_reject_reason, parse_origins

    allowed = parse_origins("")
    assert allowed == set()
    # /ext half: ANY origin passes the hello check when the list is empty. Under
    # enrollment (issue #35) auth is by the resolved instance id, not a shared token;
    # a non-None id means the secret already matched an active instance in the channel.
    hello = {
        "protocolVersion": 1, "instanceId": "i1",
        "origin": "chrome-extension://whatever-id",
    }
    assert hello_reject_reason(hello, 1, "i1", allowed) is None
    # …and a NON-empty list that does not contain it is rejected with 'origin', which
    # is what makes a real mismatch visible in the status row.
    assert hello_reject_reason(hello, 1, "i1", {GOOD_ORIGIN}) == "origin"

    # CORS half: the same empty value emits no ACAO for anybody, and the rejection is
    # counted into curator_auth_rejections_total (the audible signal for the mismatch).
    app = create_app(_settings(tmp_path, ext_allowed_origins=""))
    with TestClient(app) as client:
        before = auth_rejections.by_reason()
        r = _preflight(client, GOOD_ORIGIN)
        assert r.status_code == 400
        assert "access-control-allow-origin" not in r.headers
        after = auth_rejections.by_reason()
        assert after.get("cors_preflight", 0) > before.get("cors_preflight", 0)
