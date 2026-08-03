from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from conftest import make_settings
from starlette.testclient import TestClient

from src.app import create_app, require_operational
from src.settings import UNKNOWN_REVISION


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited.
    """
    return make_settings(tmp_path, **over)


def test_healthz_ok_even_when_degraded(tmp_path):
    # /healthz is liveness ONLY (§12): it must return 200 regardless of degraded
    # state so an orchestrator keeps routing to the container. The build revision rides
    # along but is NOT a health signal — `status` stays "ok" either way.
    app = create_app(_settings(tmp_path, build_revision="deadbeef"))
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "revision": "deadbeef"}

        client.app.state.degraded = True
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "revision": "deadbeef"}


def test_healthz_reports_the_build_revision_without_a_token(tmp_path):
    """The revision is readable with NO credential at all (acc: «доступно без токена»).

    This is the whole point of putting it here rather than on /metrics or /admin: the
    question "is this the new code?" is asked while something is already broken, and a
    diagnostic that first demands a token is one that does not get used. Reddens if the
    field is dropped, renamed, or moved behind an auth gate.
    """
    app = create_app(_settings(tmp_path, build_revision="9f1c2b3"))
    with TestClient(app) as client:
        r = client.get("/healthz")  # no Authorization header, no cookie
        assert r.status_code == 200
        assert r.json()["revision"] == "9f1c2b3"


def test_healthz_says_unknown_when_the_build_carried_no_revision(tmp_path):
    """An un-stamped build (a local `make run`) answers "unknown" — not "", not a 500.

    A missing revision is a normal state, not a failure: `make run` has no sha to bake.
    What it must never be is an empty string, which reads as a broken endpoint rather than
    as "this build cannot tell you".
    """
    app = create_app(_settings(tmp_path, build_revision=UNKNOWN_REVISION))
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "revision": "unknown"}


def test_healthz_stays_a_usable_liveness_probe(tmp_path):
    """docker-compose runs `curl -s -f -o /dev/null .../healthz` every 15s.

    `curl -f` keys on the STATUS, and `-o /dev/null` throws the body away — so the probe
    only cares that the route answers 2xx, cheaply, with no auth and no DB access. Pinned
    here because the revision field arrived on the probe's own route: it must stay a
    constant already in memory, never a lookup that could make the probe slow or flaky.
    """
    app = create_app(_settings(tmp_path, build_revision="abc123"))
    with TestClient(app) as client:
        # No credential, no side effects, and repeatable — as the healthcheck runs it.
        for _ in range(3):
            r = client.get("/healthz")
            assert r.status_code == 200
        # The DB is never touched: closing it must not affect the probe (a probe that
        # depends on the DB reports "dead" for a service that is merely degraded, §12).
        client.app.state.db = None
        assert client.get("/healthz").status_code == 200


def test_require_operational_503_in_degraded():
    # The helper later phases use to gate mutating endpoints.
    async def probe(request):
        require_operational(request)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/probe", probe, methods=["GET"])])
    app.state.degraded = False
    with TestClient(app) as client:
        assert client.get("/probe").status_code == 200
        client.app.state.degraded = True
        assert client.get("/probe").status_code == 503
