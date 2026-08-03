"""GET /api/actions (§10): the archive list with filters, deferred hidden by default.

No websocket needed — seed rows directly and assert the filtered/paginated view.
"""

import sqlite3
from types import SimpleNamespace

from conftest import make_settings
from starlette.testclient import TestClient

from src.app import create_app
from src.db.actions import insert_action

ADMIN_TOKEN = "test-admin-token"
# /api/* accepts either an admin (ADMIN_TOKEN) or an active-instance secret (issue #35 §4).
# The generic tests here just need a valid caller, so they use the admin credential;
# the force/pause tests that must EXECUTE a forced verb switch to an instance secret.
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
    return make_settings(tmp_path, **{**{
            "pass_interval_min": 5,
        }, **over})


def _seed(db_path, **kw):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        kw.setdefault("ts", 1_000_000)
        aid = insert_action(conn, **kw)
        conn.commit()
        return aid
    finally:
        conn.close()


def test_actions_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/actions").status_code == 401
        client.app.state.degraded = True
        assert client.get("/api/actions", headers=AUTH).status_code == 503


def test_actions_newest_first_and_total(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        a1 = _seed(db_path, ts=100, kind="dedupe_close", status="done", initiator="curator",
                   instance_from="i1", url="https://a/1", url_norm="https://a/1")
        a2 = _seed(db_path, ts=300, kind="dedupe_close", status="done", initiator="curator",
                   instance_from="i1", url="https://a/2", url_norm="https://a/2")
        a3 = _seed(db_path, ts=200, kind="dedupe_close", status="done", initiator="curator",
                   instance_from="i1", url="https://a/3", url_norm="https://a/3")
        body = client.get("/api/actions", headers=AUTH).json()
        assert body["total"] == 3
        assert [i["id"] for i in body["items"]] == [a2, a3, a1]  # newest ts first


def test_actions_deferred_hidden_by_default(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        done = _seed(db_path, kind="dedupe_close", status="done", initiator="curator",
                     instance_from="i1", url="https://a/1", url_norm="https://a/1")
        deferred = _seed(db_path, kind="relocate", status="deferred", initiator="curator",
                         instance_to="i2", reason="3")
        # Default: deferred rows are HIDDEN (drop the status!='deferred' filter and the
        # deferred row leaks into the default view — this reddens).
        body = client.get("/api/actions", headers=AUTH).json()
        assert [i["id"] for i in body["items"]] == [done]
        assert body["total"] == 1
        # Opt-in: ?deferred=1 returns both.
        body2 = client.get("/api/actions?deferred=1", headers=AUTH).json()
        ids = {i["id"] for i in body2["items"]}
        assert ids == {done, deferred}
        assert body2["total"] == 2


def test_actions_filters(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed(db_path, ts=100, pass_id="pX", kind="dedupe_close", status="done", initiator="curator",
              instance_from="i1", instance_to="i2", url="https://a/1?utm=1", url_norm="https://a/1")
        _seed(db_path, ts=200, pass_id="pY", kind="singleton_close", status="done", initiator="curator",
              instance_from="i3", url="https://b/2", url_norm="https://b/2")
        _seed(db_path, ts=300, pass_id="pX", kind="relocate", status="done", initiator="curator",
              instance_from="i5", instance_to="i1", url="https://c/3", url_norm="https://c/3")

        # kind filter.
        r = client.get("/api/actions?kind=singleton_close", headers=AUTH).json()
        assert [i["url_norm"] for i in r["items"]] == ["https://b/2"]

        # pass_id filter (two rows in pX).
        r = client.get("/api/actions?pass_id=pX", headers=AUTH).json()
        assert r["total"] == 2

        # instance matches EITHER side of a move (i1 is from in row1, to in row3).
        r = client.get("/api/actions?instance=i1", headers=AUTH).json()
        assert {i["url_norm"] for i in r["items"]} == {"https://a/1", "https://c/3"}

        # url filter matches by url_norm even when a full URL (with query) is passed.
        r = client.get("/api/actions?url=https://a/1?utm=99", headers=AUTH).json()
        assert [i["url_norm"] for i in r["items"]] == ["https://a/1"]

        # since filter: ts >= 250.
        r = client.get("/api/actions?since=250", headers=AUTH).json()
        assert [i["ts"] for i in r["items"]] == [300]


def test_actions_limit_offset_pagination(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ids = [
            _seed(db_path, ts=t, kind="dedupe_close", status="done", initiator="curator",
                  instance_from="i1", url=f"https://a/{t}", url_norm=f"https://a/{t}")
            for t in range(1, 6)
        ]
        # Newest first => ts 5,4,3,2,1. Page 1 (limit 2).
        p1 = client.get("/api/actions?limit=2&offset=0", headers=AUTH).json()
        assert [i["ts"] for i in p1["items"]] == [5, 4]
        assert p1["total"] == 5   # total ignores pagination
        # Page 2.
        p2 = client.get("/api/actions?limit=2&offset=2", headers=AUTH).json()
        assert [i["ts"] for i in p2["items"]] == [3, 2]
        assert len(ids) == 5
