"""``GET/POST/DELETE /api/exemptions`` (§10) — the human's «не трогать до …».

The table and the pass-side reader have existed since the schema landed; only
``restore`` ever wrote to it, so the human could neither set a protection nor see /
lift the ones restore had written. These tests pin the contract AND the property that
actually matters: a row written through the endpoint is honoured by the SAME step-4
guard the pass runs (:func:`src.curator.decide.step4_passes`), matched on the
NORMALIZED url — otherwise the endpoint would write rows nobody reads.
"""

import sqlite3
import time
from types import SimpleNamespace

from conftest import make_settings
from starlette.testclient import TestClient

from src.app import create_app

ADMIN_TOKEN = "test-admin-token"
# /api/* accepts either an admin (ADMIN_TOKEN) or an active-instance secret (issue #35 §4).
# The generic tests here just need a valid caller, so they use the admin credential;
# the force/pause tests that must EXECUTE a forced verb switch to an instance secret.
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _now_ms():
    return int(time.time() * 1000)


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
    return make_settings(tmp_path, **{**{
            "cmd_timeout_ms": 1000,
            "snapshot_timeout_ms": 200,
            "state_fresh_ms": 3000,
        }, **over})


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _seed_instance(db_path, iid):
    c = _conn(db_path)
    try:
        c.execute("INSERT INTO instances (id, connected, status) VALUES (?, 1, 'active')", (iid,))
        c.commit()
    finally:
        c.close()


def _rows(db_path):
    c = _conn(db_path)
    try:
        return c.execute(
            "SELECT instance_id, url, until, reason FROM exemptions"
        ).fetchall()
    finally:
        c.close()


# --- guards ------------------------------------------------------------------
def test_exemptions_require_bearer_and_refuse_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/exemptions").status_code == 401
        assert client.post("/api/exemptions", json={}).status_code == 401
        assert client.delete("/api/exemptions").status_code == 401
        client.app.state.degraded = True
        assert client.get("/api/exemptions", headers=AUTH).status_code == 503


# --- POST: create, refresh, validate ----------------------------------------
def test_post_creates_and_refreshes_by_instance_and_url(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        r = client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://grafana.lc/d/1", "minutes": 30},
        )
        assert r.status_code == 201
        ex = r.json()["exemption"]
        assert ex["instance_id"] == "prox" and ex["reason"] == "manual"
        assert ex["until"] > _now_ms()
        assert len(_rows(db_path)) == 1

        # A second POST for the SAME pair REFRESHES the deadline — the table's PK is
        # (instance_id, url), so "extend it by another hour" must not grow a duplicate.
        r2 = client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://grafana.lc/d/1",
                  "minutes": 600, "reason": "renovating"},
        )
        assert r2.status_code == 201
        rows = _rows(db_path)
        assert len(rows) == 1
        assert rows[0][2] > ex["until"] and rows[0][3] == "renovating"


def test_exemption_to_revoked_rejected_but_x_to_main_allowed(tmp_path):
    """issue #35 §6 cascade at the ``exemptions`` consumer of ``known_instance_ids``: an
    exemption on a REVOKED instance is rejected while one on MAIN (no active row) is
    accepted — the consumer exempts MAIN. Reverting the active-only filter makes the
    revoked target pass (the ``== 422`` reddens); dropping the main exemption makes MAIN a
    422 (the ``== 201`` reddens)."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        c = _conn(db_path)
        c.execute("UPDATE instances SET status='revoked' WHERE id='prox'")
        c.commit()
        c.close()
        # Revoked target => 422.
        assert client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://a", "minutes": 5},
        ).status_code == 422
        # X -> main (no active main row) => allowed by the main exemption.
        assert client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "main", "url": "https://a", "minutes": 5},
        ).status_code == 201


def test_post_validates_target_and_deadline(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        bad = [
            {},                                                     # no target at all
            {"instance_id": "ghost", "url": "https://a", "minutes": 5},   # unknown inst
            {"instance_id": "prox", "minutes": 5},                        # no url
            {"instance_id": "prox", "url": "https://a"},                  # no deadline
            {"instance_id": "prox", "url": "https://a", "minutes": 0},    # not future
            {"instance_id": "prox", "url": "https://a", "until": _now_ms() - 1},
            {"instance_id": "prox", "url": "https://a", "minutes": True},  # bool != int
        ]
        for body in bad:
            assert client.post("/api/exemptions", headers=AUTH, json=body).status_code == 422
        assert _rows(db_path) == []

        # An absurd horizon is CLAMPED, never stored as given: a protection that never
        # expires would retire a URL from curation for good.
        r = client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://a", "minutes": 10**9},
        )
        assert r.status_code == 201
        assert r.json()["exemption"]["until"] <= _now_ms() + 31 * 24 * 3600 * 1000


# --- GET: active by default, expired on request ------------------------------
def test_get_lists_active_and_optionally_expired(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        c = _conn(db_path)
        c.execute(
            "INSERT INTO exemptions (instance_id, url, until, reason) VALUES "
            "('prox', 'https://live/x', ?, 'restore'), "
            "('prox', 'https://dead/x', ?, 'manual')",
            (_now_ms() + 600_000, _now_ms() - 600_000),
        )
        c.commit(); c.close()

        active = client.get("/api/exemptions", headers=AUTH).json()
        assert [e["url"] for e in active["exemptions"]] == ["https://live/x"]
        assert active["exemptions"][0]["expired"] is False
        assert active["exemptions"][0]["reason"] == "restore"

        # Expired rows are not deleted eagerly, so "it WAS protected until 14:20" stays
        # answerable — but only when asked for.
        both = client.get("/api/exemptions?include_expired=1", headers=AUTH).json()
        urls = {e["url"]: e["expired"] for e in both["exemptions"]}
        assert urls == {"https://live/x": False, "https://dead/x": True}


# --- DELETE: idempotent lift -------------------------------------------------
def test_delete_lifts_and_is_idempotent(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://a/b", "minutes": 30},
        )
        r = client.request(
            "DELETE", "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://a/b"},
        )
        assert r.status_code == 200 and r.json()["deleted"] == 1
        assert _rows(db_path) == []
        # Repeating a successful lift must not become an error (a retried click).
        again = client.request(
            "DELETE", "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://a/b"},
        )
        assert again.status_code == 200 and again.json()["deleted"] == 0
        # The query-string form works too (httpx's `delete` shorthand takes no body).
        assert client.delete(
            "/api/exemptions?instance_id=prox&url=https://a/b", headers=AUTH
        ).status_code == 200


# --- the property that matters: the pass actually honours the row ------------
def test_a_posted_exemption_is_honoured_by_the_pass_step4_guard(tmp_path):
    """An endpoint that writes rows nobody reads would be worthless. This runs the
    REAL :func:`src.curator.decide.step4_passes` — the guard the pass uses — against a
    tab the endpoint just protected, and matches on the NORMALIZED url (the archived /
    live addresses differ by tracking params all the time)."""
    from src.curator.decide import step4_passes

    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        until = _now_ms() + 600_000
        client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://grafana.lc/d/1", "until": until},
        )
        rows = client.get("/api/exemptions", headers=AUTH).json()["exemptions"]

        now = _now_ms()
        tab = SimpleNamespace(
            instance_id="prox", tab_id=1, window_id=1,
            url="https://grafana.lc/d/1?utm_source=x",     # same tab, different query
            pinned=0, audible=0, active=0, age_unknown=0,
            last_active_at=now - 10 * 3600_000, opened_at=now - 10 * 3600_000,
        )
        mirror = SimpleNamespace(
            instances={"prox": SimpleNamespace(focused_window_id=None)},
            windows={("prox", 1): ("normal", "normal")},
            exemptions=[(e["instance_id"], e["url"], e["until"]) for e in rows],
            quarantine=[],
        )
        assert step4_passes(tab, mirror, now, 3600_000) is False   # protected

        # Non-vacuity: lift it and the very same tab becomes closable again.
        client.request(
            "DELETE", "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://grafana.lc/d/1"},
        )
        mirror.exemptions = [
            (e["instance_id"], e["url"], e["until"])
            for e in client.get("/api/exemptions", headers=AUTH).json()["exemptions"]
        ]
        assert step4_passes(tab, mirror, now, 3600_000) is True


# --- the URL must be one the pass can ever match ----------------------------
def test_post_rejects_a_url_the_pass_could_never_match(tmp_path):
    """A scheme-less address is the one a human actually copies — Chrome's address bar
    shows ``grafana.lc/d/1``, not ``https://grafana.lc/d/1``.

    The pass matches ``normalize_url(exemption.url) == normalize_url(tab.url)``, and
    ``normalize_url`` is ``urlsplit``-based: without a scheme the whole string lands in
    ``path`` and normalises to ``grafana.lc/d/1``, which can never equal the live tab's
    ``https://grafana.lc/d/1``. Accepting it produced a 201 and a row ``GET`` lists as
    ACTIVE, while the next pass closed the very tab the human had just protected.

    The test proves the failure it prevents, not just the status code: the rejected
    string is fed through the real matcher to show it never matches."""
    from src.db.actions import normalize_url

    # The concrete trap, stated as an assertion rather than as prose.
    assert normalize_url("grafana.lc/d/1") != normalize_url("https://grafana.lc/d/1")

    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        for bad in (
            "grafana.lc/d/1",          # the copied-from-the-address-bar case
            "//grafana.lc/d/1",        # scheme-relative
            "file:///etc/passwd",      # not http(s)
            "javascript:alert(1)",
            "data:text/html,x",
            "https://",                # scheme, no host
            "   ",
        ):
            r = client.post(
                "/api/exemptions", headers=AUTH,
                json={"instance_id": "prox", "url": bad, "minutes": 30},
            )
            assert r.status_code == 422, f"{bad!r} was accepted"
        assert _rows(db_path) == []

        # The corrected form is accepted, so the rule is "must be a real URL", not
        # "reject everything".
        assert client.post(
            "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "https://grafana.lc/d/1", "minutes": 30},
        ).status_code == 201


def test_delete_rejects_the_same_shapes(tmp_path):
    # DELETE validates identically — otherwise the lift path would accept a key the
    # create path cannot produce, which can only ever be a no-op.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        r = client.request(
            "DELETE", "/api/exemptions", headers=AUTH,
            json={"instance_id": "prox", "url": "grafana.lc/d/1"},
        )
        assert r.status_code == 422


# --- §7 pause gate, both mutating verbs -------------------------------------
def test_exemption_mutations_are_gated_by_pause(tmp_path):
    """§7: a pause silences the mutating verbs. Editing the protection policy is not one
    of the human's own BUTTONS (§7's exception covers focus / restore / undo /
    merge_windows), so no ``force`` here either — reddens if the gate is dropped from
    either verb, or if ``force`` starts being honoured."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        client.post("/api/pause", headers=AUTH, json={"minutes": 60})

        body = {"instance_id": "prox", "url": "https://a/b", "minutes": 30}
        for payload in (body, {**body, "force": True}):
            r = client.post("/api/exemptions", headers=AUTH, json=payload)
            assert r.status_code == 423 and r.json()["error"] == "paused"

        for payload in ({"instance_id": "prox", "url": "https://a/b"},
                        {"instance_id": "prox", "url": "https://a/b", "force": True}):
            r = client.request("DELETE", "/api/exemptions", headers=AUTH, json=payload)
            assert r.status_code == 423 and r.json()["error"] == "paused"

        assert _rows(db_path) == []
        # Reads stay open during a pause (the page must show the truth, §7).
        assert client.get("/api/exemptions", headers=AUTH).status_code == 200
