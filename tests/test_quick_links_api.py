"""POST /api/quick_links/ops (§10): add/remove/reorder, server-side position,
Idempotency-Key.
"""

import sqlite3
from types import SimpleNamespace

from conftest import make_settings
from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}


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


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, url, title, position FROM quick_links ORDER BY position, id"
        ).fetchall()
    finally:
        conn.close()


def test_ops_requires_bearer_and_refuses_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/api/quick_links/ops", json=[]).status_code == 401
        client.app.state.degraded = True
        assert client.post(
            "/api/quick_links/ops", json=[], headers=AUTH
        ).status_code == 503


def test_ops_body_must_be_array(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post(
            "/api/quick_links/ops", json={"op": "add"}, headers=AUTH
        ).status_code == 400


# --- add: server-side position (append), ignores any client position --------
def test_add_appends_server_side_position(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # A client-supplied position is IGNORED — the server appends (§10).
        resp = client.post(
            "/api/quick_links/ops",
            json=[
                {"op": "add", "url": "https://a", "title": "A", "position": 99},
                {"op": "add", "url": "https://b", "title": "B", "position": 5},
            ],
            headers=AUTH,
        )
        assert resp.status_code == 200
        links = resp.json()["quick_links"]
        # Appended in arrival order at server-assigned positions 0,1 — NOT 99/5.
        assert [(l["url"], l["position"]) for l in links] == [
            ("https://a", 0),
            ("https://b", 1),
        ]
        rows = _rows(db_path)
        assert [r["position"] for r in rows] == [0, 1]


# --- remove ------------------------------------------------------------------
def test_remove_by_url_and_by_id(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        client.post(
            "/api/quick_links/ops",
            json=[{"op": "add", "url": "https://a"}, {"op": "add", "url": "https://b"}],
            headers=AUTH,
        )
        # remove by url
        r = client.post(
            "/api/quick_links/ops",
            json=[{"op": "remove", "url": "https://a"}],
            headers=AUTH,
        )
        urls = [l["url"] for l in r.json()["quick_links"]]
        assert urls == ["https://b"]
        b_id = r.json()["quick_links"][0]["id"]
        # remove by id
        r2 = client.post(
            "/api/quick_links/ops", json=[{"op": "remove", "id": b_id}], headers=AUTH
        )
        assert r2.json()["quick_links"] == []


# --- reorder: explicit reposition op ----------------------------------------
def test_reorder_sets_positions(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        r = client.post(
            "/api/quick_links/ops",
            json=[
                {"op": "add", "url": "https://a"},
                {"op": "add", "url": "https://b"},
                {"op": "add", "url": "https://c"},
            ],
            headers=AUTH,
        )
        by_url = {l["url"]: l["id"] for l in r.json()["quick_links"]}
        # Reorder to c, a, b.
        r2 = client.post(
            "/api/quick_links/ops",
            json=[{"op": "reorder", "order": [by_url["https://c"], by_url["https://a"], by_url["https://b"]]}],
            headers=AUTH,
        )
        assert [l["url"] for l in r2.json()["quick_links"]] == [
            "https://c",
            "https://a",
            "https://b",
        ]


# --- idempotency: same key => second flush is a no-op -----------------------
def test_idempotency_key_dedupes_a_retried_flush(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        headers = {**AUTH, "Idempotency-Key": "batch-1"}
        r1 = client.post(
            "/api/quick_links/ops",
            json=[{"op": "add", "url": "https://a", "title": "A"}],
            headers=headers,
        )
        assert [l["url"] for l in r1.json()["quick_links"]] == ["https://a"]

        # A retry of the SAME batch key carrying a DIFFERENT op must be ignored: the
        # server already applied batch-1. Remove the idempotency guard and "https://b"
        # would be added (the mutation reddens).
        r2 = client.post(
            "/api/quick_links/ops",
            json=[{"op": "add", "url": "https://b", "title": "B"}],
            headers=headers,
        )
        assert [l["url"] for l in r2.json()["quick_links"]] == ["https://a"]

        # A NEW key DOES apply — proving the dedupe is per-key, not a blanket freeze.
        r3 = client.post(
            "/api/quick_links/ops",
            json=[{"op": "add", "url": "https://c", "title": "C"}],
            headers={**AUTH, "Idempotency-Key": "batch-2"},
        )
        assert [l["url"] for l in r3.json()["quick_links"]] == ["https://a", "https://c"]
