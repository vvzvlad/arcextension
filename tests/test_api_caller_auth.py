"""``require_api_caller`` — the per-caller ``/api/*`` auth model (issue #35 §4/§5).

Two layers:

* unit tests over :func:`src.api.guards.require_api_caller` with a fake request, pinning
  the ORDER guarantees that are awkward to observe end-to-end — ADMIN-first (no DB
  touch), the ``status='active'`` filter, and DB-error → 503 (never a silent pass);
* integration tests over a real app pinning the acceptance rows: an instance secret opens
  ``/api/state`` (acc 6), and ``/api/focus {force:true}`` crosses an armed pause ONLY for
  the instance caller, never for ADMIN_TOKEN (acc 7).
"""

import sqlite3
import time
from types import SimpleNamespace

import pytest
from conftest import (
    ADMIN_TOKEN,
    admin_headers,
    approve_instance,
    instance_headers,
    make_settings,
    secret_hash_for,
)
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from src.api.guards import Caller, require_api_caller
from src.app import create_app


# --- unit: a fake request just rich enough for require_api_caller --------------
def _fake_request(token, *, db_read, admin_token=ADMIN_TOKEN):
    """A stand-in Request: an Authorization header, ``app.state.settings.admin_token``
    and an async ``app.state.db.read`` (``db_read`` is the sync fn(conn) result or an
    exception factory the fake read applies)."""

    async def _read(fn):
        return db_read(fn)

    return SimpleNamespace(
        headers={"authorization": f"Bearer {token}"} if token is not None else {},
        state=SimpleNamespace(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(admin_token=admin_token),
                db=SimpleNamespace(read=_read),
            )
        ),
    )


@pytest.mark.asyncio
async def test_admin_token_resolves_admin_without_touching_the_db():
    """ADMIN first (§4): a token equal to ADMIN_TOKEN is the admin caller BEFORE any DB
    read — proven by a db.read that raises: it must never be called."""

    def _boom(fn):
        raise AssertionError("db.read must NOT run for an admin token")

    req = _fake_request(ADMIN_TOKEN, db_read=_boom)
    caller = await require_api_caller(req)
    assert caller == Caller(kind="admin", instance_id=None)
    assert req.state.caller == caller


@pytest.mark.asyncio
async def test_admin_wins_even_if_the_token_also_hashes_to_an_instance():
    """Admin-first is unconditional: even if the same bytes ALSO resolve to an active
    instance, the admin branch returns first (it never reaches the DB)."""

    def _would_match(fn):
        raise AssertionError("admin must win before the instance lookup")

    caller = await require_api_caller(_fake_request(ADMIN_TOKEN, db_read=_would_match))
    assert caller.kind == "admin"


@pytest.mark.asyncio
async def test_active_instance_secret_resolves_instance():
    caller = await require_api_caller(
        _fake_request("some-secret-hash", db_read=lambda fn: ("i1", "active"))
    )
    assert caller == Caller(kind="instance", instance_id="i1")


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [None, ("i1", "revoked"), ("i1", "pending")])
async def test_unknown_or_inactive_secret_is_401(resolved):
    """No admin match and no ACTIVE instance (unknown / revoked / pending) → 401."""
    with pytest.raises(HTTPException) as ei:
        await require_api_caller(
            _fake_request("nope", db_read=lambda fn: resolved)
        )
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_or_non_bearer_header_is_401():
    with pytest.raises(HTTPException) as ei:
        await require_api_caller(_fake_request(None, db_read=lambda fn: None))
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_db_error_during_resolution_is_503_never_a_pass():
    """A DB failure in the revocation lookup is 503 — it must NOT fall through to an
    anonymous or admin pass (revocation we cannot check fails CLOSED)."""

    def _raise(fn):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(HTTPException) as ei:
        await require_api_caller(_fake_request("some-secret", db_read=_raise))
    assert ei.value.status_code == 503


# --- integration: real app ----------------------------------------------------
def _arm_pause(db_path, until_ms=None):
    until = until_ms if until_ms is not None else int(time.time() * 1000) + 3_600_000
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('pause_until', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(until),),
        )
        conn.commit()
    finally:
        conn.close()


def test_instance_secret_and_admin_both_open_api_state(tmp_path):
    """Acc 6: an ACTIVE instance's secretHash opens ``GET /api/state`` (200), and so does
    ADMIN_TOKEN. The secret is the SAME credential the client sends on /ext hello."""
    app = create_app(make_settings(tmp_path, pass_interval_min=100_000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "i1")  # active row with secret_hash_for('i1')
        assert client.get(
            "/api/state", headers=instance_headers(secret_hash_for("i1"))
        ).status_code == 200
        assert client.get("/api/state", headers=admin_headers()).status_code == 200


def test_revoked_and_unknown_secret_rejected_on_api(tmp_path):
    """A revoked instance's secret and a wholly unknown token are both 401 on ``/api/*``
    (revocation acts instantly — no caching)."""
    app = create_app(make_settings(tmp_path, pass_interval_min=100_000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "gone", status="revoked")
        assert client.get(
            "/api/state", headers=instance_headers(secret_hash_for("gone"))
        ).status_code == 401
        assert client.get(
            "/api/state", headers=instance_headers("deadbeef-never-seen")
        ).status_code == 401


def test_db_error_makes_api_state_503_not_401(tmp_path):
    """A DB-read failure inside ``require_api_caller`` surfaces as 503 through the real
    endpoint — never 401 (which would leak "unknown") nor 200 (a silent pass)."""
    app = create_app(make_settings(tmp_path, pass_interval_min=100_000))
    with TestClient(app) as client:
        def _raise(_fn):
            raise sqlite3.OperationalError("simulated DB outage")

        # Patch the live async db.read to fail; a non-admin token reaches it.
        async def _read(fn):
            _raise(fn)

        client.app.state.db.read = _read
        resp = client.get(
            "/api/state", headers=instance_headers("any-non-admin-token")
        )
        assert resp.status_code == 503


def test_focus_force_crosses_pause_only_for_instance_not_admin(tmp_path):
    """Acc 7: at an armed pause, ``POST /api/focus {force:true}`` is 423 for an ADMIN_TOKEN
    caller (an agent must not bypass §7 outside MCP) but crosses the gate for the instance
    caller (the human) — it then fails on its own merits (no live socket → 502), which is
    exactly the proof the PAUSE no longer decided."""
    app = create_app(make_settings(tmp_path, pass_interval_min=100_000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        approve_instance(db_path, "main")
        _arm_pause(db_path)

        # Admin's force:true is refused by the pause.
        admin_resp = client.post(
            "/api/focus", headers=admin_headers(),
            json={"instance": "main", "tabId": 1, "force": True},
        )
        assert admin_resp.status_code == 423
        assert admin_resp.json()["error"] == "paused"

        # The instance caller crosses the gate; with no live socket it 502s, NOT 423.
        inst_resp = client.post(
            "/api/focus", headers=instance_headers(secret_hash_for("main")),
            json={"instance": "main", "tabId": 1, "force": True},
        )
        assert inst_resp.status_code != 423
