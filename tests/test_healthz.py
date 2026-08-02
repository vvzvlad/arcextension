from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.app import create_app, require_operational


def _settings(tmp_path):
    return SimpleNamespace(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0",
        port=8000,
    )


def test_healthz_ok_even_when_degraded(tmp_path):
    # /healthz is liveness ONLY (§12): it must return 200 regardless of degraded
    # state so an orchestrator keeps routing to the container.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        client.app.state.degraded = True
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


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
