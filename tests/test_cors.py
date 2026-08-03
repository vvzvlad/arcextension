"""CORS on /api/* (§12): ANY origin is allowed, and credentials stay OFF.

The startpage fetches /api/* cross-origin with an Authorization Bearer header, which
forces a CORS preflight. The old ``EXT_ALLOWED_ORIGINS`` allow-list — and with it the
"never emit ``*``" invariant — was removed on purpose (``src/api/cors.py`` carries the
argument: /api/* is already behind ``require_api_caller``, credentials are off so there is
no ambient session to ride, CORS binds browsers only, and the service is not reachable
from the internet). These tests pin what replaced it, end-to-end through the real
``create_app`` middleware stack, plus the two acceptance auth gates (/metrics needs
METRICS_TOKEN, /api/* needs ADMIN_TOKEN or an active-instance secret — never
METRICS_TOKEN).

Each test reddens if its guard is removed: dropping the CORSMiddleware drops the ACAO
header (test 1), and turning ``allow_credentials`` back on would make the wildcard
either illegal or dangerous (test 4).
"""

from conftest import make_settings
from starlette.testclient import TestClient

from src.app import create_app

ADMIN_TOKEN = "test-admin-token"
METRICS_TOKEN = "test-metrics-token"
EXT_ORIGIN = "chrome-extension://abc"
OTHER_EXT_ORIGIN = "chrome-extension://zzz"  # a different extension id
FOREIGN_ORIGIN = "https://some-other-site.example"
# /api/* now authenticates a caller (issue #35 §4); the CORS behaviour is orthogonal, so
# the generic admin credential is used to reach a 200.
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
    return make_settings(tmp_path, **{**{"pass_interval_min": 5}, **over})


def _preflight(client, origin, path="/api/state", method="GET", headers="authorization,content-type"):
    request_headers = {"Origin": origin, "Access-Control-Request-Method": method}
    if headers is not None:
        request_headers["Access-Control-Request-Headers"] = headers
    return client.options(path, headers=request_headers)


# --- any origin passes the preflight -----------------------------------------
def test_preflight_from_any_origin_is_allowed(tmp_path):
    # Every id the fleet can produce, plus a page that is not an extension at all: the
    # allow-list is gone, so all of them get through. Redden: reinstate an allow-list and
    # the second and third of these start returning 400.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        for origin in (EXT_ORIGIN, OTHER_EXT_ORIGIN, FOREIGN_ORIGIN):
            r = _preflight(client, origin)
            assert r.status_code == 200, origin
            assert r.headers["access-control-allow-origin"] == "*"


def test_simple_get_carries_the_wildcard_acao(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = client.get("/api/state", headers={**AUTH, "Origin": EXT_ORIGIN})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"


def test_an_unpinned_extension_id_is_no_longer_a_failure_mode(tmp_path):
    """The §12 «бесшумный отказ» this file used to pin no longer exists.

    An unpacked extension's id is the hash of its load path, so moving or renaming the
    bundle changed the origin and used to cut the startpage off at the preflight while the
    websocket stayed up and the instance looked green. With no allow-list there is nothing
    for the id to disagree with. Redden: bring the allow-list back and one of these two
    ids stops being served.
    """
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        for origin in (EXT_ORIGIN, OTHER_EXT_ORIGIN):
            assert _preflight(client, origin).status_code == 200


# --- the invariant that DID survive: credentials stay off --------------------
def test_credentials_are_never_allowed(tmp_path):
    """``allow_credentials=False`` is what makes the wildcard safe, so it is pinned here.

    With credentials off no cookie / TLS client cert / HTTP-auth is ever attached to a
    cross-origin call, so a foreign page that reaches /api/* still carries no credential
    and still gets 401. Redden: set ``allow_credentials=True`` in ``cors_kwargs`` and the
    header below appears (and Starlette stops echoing a bare ``*``).
    """
    from src.api.cors import cors_kwargs

    assert cors_kwargs()["allow_credentials"] is False
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = _preflight(client, FOREIGN_ORIGIN)
        assert "access-control-allow-credentials" not in r.headers
        # …and the wildcard is a real wildcard, not an echo of the requesting origin.
        assert r.headers["access-control-allow-origin"] == "*"


def test_a_foreign_origin_still_cannot_read_api_without_a_token(tmp_path):
    # The point of the whole change: the lock on /api/* is require_api_caller, not CORS.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = client.get("/api/state", headers={"Origin": FOREIGN_ORIGIN})
        assert r.status_code == 401


# --- the preflight can still be REJECTED, on method/header -------------------
def test_preflight_with_an_undeclared_method_is_rejected_and_counted(tmp_path):
    """``cors_preflight`` did NOT become unreachable when origins were opened up.

    Starlette refuses a preflight on three grounds — origin, method, header — and only
    the origin ground is gone. A verb outside ``_ALLOW_METHODS`` (or a header outside
    ``_ALLOW_HEADERS``) is still a 400, and it is still the §12 silent-failure shape: the
    socket stays up, the instance stays green, only the fetch dies. That is why
    ``CountingCORSMiddleware`` and its alert rule are kept. Redden: delete the counting
    override and the counter stops moving.
    """
    from src.api.auth_metrics import auth_rejections

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        before = auth_rejections.by_reason()
        r = _preflight(client, EXT_ORIGIN, method="PATCH")
        assert r.status_code == 400
        after = auth_rejections.by_reason()
        assert after.get("cors_preflight", 0) > before.get("cors_preflight", 0)


def test_preflight_with_an_undeclared_header_is_rejected(tmp_path):
    # The other reachable branch: a custom header added to the startpage's fetch without
    # being added to _ALLOW_HEADERS. Redden: widen allow_headers to "*" and this passes,
    # which would ALSO make the counter above unreachable.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = _preflight(client, EXT_ORIGIN, headers="authorization,x-not-declared")
        assert r.status_code == 400


# --- acceptance auth gates (pinned here too) ---------------------------------
def test_metrics_requires_metrics_token(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        # ADMIN_TOKEN must NOT open /metrics (§12: separate read-only credential).
        assert client.get(
            "/metrics", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        ).status_code == 401
        assert client.get(
            "/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}
        ).status_code == 200


def test_api_rejects_unauthenticated_and_metrics_token(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # read
        assert client.get("/api/state").status_code == 401
        # write
        assert client.post(
            "/api/focus", json={"instance": "i1", "tabId": 1}
        ).status_code == 401
        # METRICS_TOKEN must NOT open /api/* (§12: it opens /metrics only; /api/* takes
        # ADMIN_TOKEN or an active-instance secretHash, §13).
        assert client.get(
            "/api/state", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}
        ).status_code == 401
