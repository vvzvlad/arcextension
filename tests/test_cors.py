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

from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
METRICS_TOKEN = "test-metrics-token"
GOOD_ORIGIN = "chrome-extension://abc"
EVIL_ORIGIN = "https://evil.com"
OTHER_EXT_ORIGIN = "chrome-extension://zzz"  # a non-listed extension id
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0",
        port=8000,
        heartbeat_ms=600_000,
        protocol_version=1,
        ext_token=EXT_TOKEN,
        metrics_token=METRICS_TOKEN,
        ext_allowed_origins=GOOD_ORIGIN,
        cmd_timeout_ms=2000,
        snapshot_timeout_ms=2000,
        state_fresh_ms=3_000_000,
        restore_exemption_min=120,
        actions_retention_days=90,
        js_audit_retention_days=730,
        pass_interval_min=5,
        idle_minutes=60,
        main_instance_id="main",
    )
    s.update(over)
    return SimpleNamespace(**s)


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
