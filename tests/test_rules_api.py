"""/api/rules CRUD + confirm_impact + /api/rules/preview (§8, §10) via TestClient.

Preview no longer reads a stale mirror: it ACTIVELY requests a fresh snapshot from
each connected instance and counts against the refreshed mirror (§8). So the count
tests connect a real websocket (like test_restore.py's `_connect_fresh`) and let it
answer the snapshot_request — there is no `state_fresh_ms=10**12` mask any more; the
suite runs with the realistic default freshness window.

Each confirm test is written so the gate reddens if its guard is removed (the SUM
threshold, the always-gated DELETE, the empty→non-empty drain, an uncountable
instance forcing confirm, the active snapshot request, the survivor ladder).
"""

import sqlite3
import threading
import time
from types import SimpleNamespace

from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}

IDLE_MS = 60 * 60_000            # idle_minutes=60 => a tab must be ~1h idle to move
OLD = 4_000_000                  # an age (ms) comfortably past IDLE_MS => guarded


def _now_ms():
    return int(time.time() * 1000)


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0",
        port=8000,
        heartbeat_ms=600_000,       # no ping interferes with the ws-driven tests
        protocol_version=1,
        ext_token=EXT_TOKEN,
        ext_allowed_origins="",
        cmd_timeout_ms=2000,
        snapshot_timeout_ms=2000,
        state_fresh_ms=3000,        # the REALISTIC default — no 10**12 masking
        idle_minutes=60,
        main_instance_id="main",
        actions_retention_days=90,
        js_audit_retention_days=730,
        pass_interval_min=5,  # curator clock guard + driver (Фаза 8 lifespan)
    )
    s.update(over)
    return SimpleNamespace(**s)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _seed_instance(db_path, iid, connected=1, snapshot_at=None, focused=None):
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO instances (id, connected, snapshot_at, focused_window_id) "
            "VALUES (?,?,?,?)",
            (iid, connected, _now_ms() if snapshot_at is None else snapshot_at, focused),
        )
        c.commit()
    finally:
        c.close()


def _seed_tab(db_path, iid, tab_id, url, title="t", pinned=0, active=0, audible=0,
              window_id=1, last_active_at=0):
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, "
            "fav_icon_url, pinned, active, opened_at, last_active_at, age_unknown, "
            "self_navigating, audible, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, tab_id, window_id, url, title, None, pinned, active, 0,
             last_active_at, 0, 0, audible, 0),
        )
        c.commit()
    finally:
        c.close()


def _seed_rule(db_path, pattern, instance_id, singleton=0, invalid=0):
    c = _conn(db_path)
    try:
        cur = c.execute(
            "INSERT INTO rules (pattern, instance_id, singleton, invalid, created_at) "
            "VALUES (?,?,?,?,1)",
            (pattern, instance_id, singleton, invalid),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


# --- websocket helpers (the extension side; mirror test_restore.py) ----------
def _hello(instance_id, session="sess-1", **over):
    msg = {
        "type": "hello",
        "protocolVersion": 1,
        "token": EXT_TOKEN,
        "instanceId": instance_id,
        "installUuid": f"uuid-{instance_id}",
        "origin": "chrome-extension://abc",
        "title": "Themed",
        "sessionId": session,
        "allowExecuteJs": False,
    }
    msg.update(over)
    return msg


def _tab(tab_id, url, title="t", window_id=1, age_ms=OLD, opened_ago_ms=OLD,
         pinned=False, active=False, audible=False, age_unknown=False):
    return {
        "tabId": tab_id, "windowId": window_id, "url": url, "title": title,
        "favIconUrl": None, "pinned": pinned, "active": active, "audible": audible,
        "ageMs": age_ms, "openedAgoMs": opened_ago_ms, "ageUnknown": age_unknown,
        "selfNavigating": False,
    }


def _snapshot(req_id, tabs, session="sess-1"):
    return {
        "type": "snapshot",
        "id": req_id,
        "sessionId": session,
        "focusedWindowId": 1,
        "tabs": tabs,
        "windows": [{"id": 1, "type": "normal", "state": "normal"}],
    }


def _db_row(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _wait_until(fn, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = fn()
        if val:
            return val
        time.sleep(interval)
    return fn()


def _connect_fresh(client, db_path, instance_id, session="sess-1", tabs=None):
    """hello + answer the initial snapshot_request so the instance is FRESH.

    After this the mirror holds ``tabs`` and preview finds the instance already
    fresh (no re-request), so the HTTP call can run inline.
    """
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    ws.receive_json()                # hello_ack
    req = ws.receive_json()          # snapshot_request
    ws.send_json(_snapshot(req["id"], tabs or [], session=session))
    _wait_until(
        lambda: _db_row(
            db_path, "SELECT snapshot_at FROM instances WHERE id=?", (instance_id,)
        )[0]
        is not None
    )
    return ws


def _connect_unanswered(client, instance_id, session="sess-1"):
    """hello + DISCARD the initial snapshot_request (leave snapshot_at NULL).

    The instance is connected but has never delivered a snapshot, so preview MUST
    actively request one — the hook the `preview-requests-snapshot` guard tests.
    """
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    ws.receive_json()                # hello_ack
    ws.receive_json()                # initial snapshot_request (discarded)
    return ws


# --- auth / degraded --------------------------------------------------------
def test_rules_require_bearer_and_refuse_degraded(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/rules").status_code == 401
        assert client.post("/api/rules", json={}).status_code == 401
        assert client.post("/api/rules/preview", json={}).status_code == 401
        client.app.state.degraded = True
        assert client.get("/api/rules", headers=AUTH).status_code == 503


# --- validation: full-URL pattern => 422 at save ----------------------------
def test_create_rejects_full_url_pattern_422(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "main")
        _seed_instance(db_path, "prox")
        resp = client.post(
            "/api/rules",
            headers=AUTH,
            json={"pattern": "https://borneo.lc/path", "instance_id": "prox"},
        )
        assert resp.status_code == 422
        # Nothing stored.
        assert client.get("/api/rules", headers=AUTH).json()["rules"] == []


def test_create_rejects_unknown_instance_422(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "main")
        resp = client.post(
            "/api/rules", headers=AUTH,
            json={"pattern": "borneo.lc", "instance_id": "ghost"},
        )
        assert resp.status_code == 422


# --- confirm_impact: only relocations (closures=0) still gated (SUM) ---------
def test_only_relocations_still_requires_confirm(tmp_path):
    # Pre-existing rule => NOT an empty-boundary change, so the ONLY reason to gate
    # is relocations>0 with closures==0: this reddens if the threshold were on
    # closures alone instead of the sum (§8 ⚠️). Both instances are connected+fresh
    # over the websocket, so counting works with the REALISTIC freshness window.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_rule(db_path, "example.com", "prox")      # existing rule (non-empty)
        ws_main = _connect_fresh(
            client, db_path, "main", tabs=[_tab(1, "https://grafana.lc/")]
        )
        ws_prox = _connect_fresh(client, db_path, "prox", session="sess-2", tabs=[])
        try:
            body = {"pattern": "grafana.lc", "instance_id": "prox"}
            resp = client.post("/api/rules", headers=AUTH, json=body)
            assert resp.status_code == 409                  # gated
            payload = resp.json()
            assert payload["relocations"] >= 1
            assert payload["closures"] == 0                 # ONLY relocations
            assert payload["requires_confirm"] is True
            assert payload["not_counted"] == []             # both instances counted
            # examples carry the title so the human recognizes the tab
            assert payload["relocation_examples"][0]["to"] == "prox"

            # Still nothing written.
            assert len(client.get("/api/rules", headers=AUTH).json()["rules"]) == 1
            # Re-submit WITH confirm => created.
            ok = client.post(
                "/api/rules", headers=AUTH, json={**body, "confirm_impact": True}
            )
            assert ok.status_code == 201
            assert len(client.get("/api/rules", headers=AUTH).json()["rules"]) == 2
        finally:
            ws_main.__exit__(None, None, None)
            ws_prox.__exit__(None, None, None)


# --- confirm_impact: empty -> non-empty enables the drain and is gated -------
def test_empty_to_nonempty_enables_drain_and_is_gated(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # An UNRULED themed tab at prox: only the empty->non-empty transition
        # (drain on) relocates it. Reddens if the drain is not modeled for the pass.
        ws_prox = _connect_fresh(
            client, db_path, "prox", tabs=[_tab(1, "https://random.io/")]
        )
        ws_main = _connect_fresh(client, db_path, "main", session="sess-2", tabs=[])
        try:
            resp = client.post(
                "/api/rules", headers=AUTH,
                json={"pattern": "other.com", "instance_id": "prox"},
            )
            assert resp.status_code == 409
            payload = resp.json()
            assert payload["enables_drain"] is True
            assert payload["relocations"] >= 1              # the unruled tab drains
            assert payload["relocation_examples"][0]["to"] == "main"
        finally:
            ws_prox.__exit__(None, None, None)
            ws_main.__exit__(None, None, None)


# --- confirm_impact: DELETE is always gated ---------------------------------
def test_delete_last_rule_is_gated_even_with_zero_modeled_impact(tmp_path):
    # Deleting the last rule disables curation entirely (§8). DELETE is ALWAYS gated
    # (disables_curation is a property of the candidate rule set, not of freshness),
    # so this holds even with no live instances — reddens if the unconditional
    # DELETE gate is dropped.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        rid = _seed_rule(db_path, "grafana.lc", "prox")

        resp = client.delete(f"/api/rules/{rid}", headers=AUTH)
        assert resp.status_code == 409
        assert resp.json()["disables_curation"] is True
        # Still present.
        assert len(client.get("/api/rules", headers=AUTH).json()["rules"]) == 1
        # With confirm => deleted.
        ok = client.request("DELETE", f"/api/rules/{rid}", headers=AUTH,
                            json={"confirm_impact": True})
        assert ok.status_code == 200
        assert client.get("/api/rules", headers=AUTH).json()["rules"] == []


def test_delete_gated_drains_held_tabs(tmp_path):
    # Deleting a rule that HELD tabs at prox drains them back to main next pass.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        rid = _seed_rule(db_path, "grafana.lc", "prox")
        _seed_rule(db_path, "other.com", "prox")            # 2nd rule => drain stays on
        ws_prox = _connect_fresh(
            client, db_path, "prox", tabs=[_tab(1, "https://grafana.lc/")]
        )
        ws_main = _connect_fresh(client, db_path, "main", session="sess-2", tabs=[])
        try:
            resp = client.request("DELETE", f"/api/rules/{rid}", headers=AUTH)
            assert resp.status_code == 409
            # After removing the grafana rule the tab is unruled at prox => drains.
            assert resp.json()["relocations"] >= 1
        finally:
            ws_prox.__exit__(None, None, None)
            ws_main.__exit__(None, None, None)


# --- preview: relocations vs closures counted SEPARATELY (dedup closure) -----
def test_preview_counts_dedupe_closure_separately(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # prox already holds the exact same full URL => the main tab is a dedupe
        # CLOSURE at the source, not a relocation.
        ws_prox = _connect_fresh(
            client, db_path, "prox", tabs=[_tab(1, "https://grafana.lc/")]
        )
        ws_main = _connect_fresh(
            client, db_path, "main", session="sess-2",
            tabs=[_tab(2, "https://grafana.lc/")],
        )
        try:
            resp = client.post(
                "/api/rules/preview", headers=AUTH,
                json={"pattern": "grafana.lc", "instance_id": "prox"},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["closures"] == 1
            assert payload["relocations"] == 0
            assert payload["closure_examples"][0]["reason"] == "dedupe_close"
        finally:
            ws_prox.__exit__(None, None, None)
            ws_main.__exit__(None, None, None)


# --- preview freshness: a disconnected instance is "not counted" -------------
def test_preview_marks_disconnected_instance_not_counted(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # prox is DISCONNECTED but its tabs mirror is NOT deleted (§8): a tab that
        # WOULD relocate must be surfaced as "not counted", never a silent zero. No
        # live socket => the active snapshot request cannot help it.
        _seed_instance(db_path, "prox", connected=0, snapshot_at=_now_ms())
        _seed_tab(db_path, "prox", 1, "https://random.io/")  # would drain if counted

        resp = client.post(
            "/api/rules/preview", headers=AUTH,
            json={"pattern": "other.com", "instance_id": "main"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        prox = next(i for i in payload["instances"] if i["id"] == "prox")
        assert prox["counted"] is False
        assert prox["connected"] is False
        assert prox["reason"] == "disconnected"
        assert prox["snapshot_at"] is not None          # freshness carried, not dropped
        # The disconnected tab did NOT silently contribute a zero-move.
        assert payload["relocations"] == 0
        # And it is surfaced in not_counted so the human sees why (WARNING 2).
        assert {"id": "prox", "reason": "disconnected"} in payload["not_counted"]


def test_preview_marks_unanswering_instance_not_counted(tmp_path):
    # Connected but does NOT answer the snapshot_request preview emits => reason
    # 'timeout', not counted. Exercises the request-then-give-up half of the active
    # refresh (never a silent stale zero). Runs inline: preview only POLLS the DB
    # while it waits, so nothing on the socket has to be driven — it just times out.
    app = create_app(_settings(tmp_path, snapshot_timeout_ms=300))
    with TestClient(app) as client:
        ws = _connect_unanswered(client, "prox")   # connected, snapshot_at NULL
        try:
            resp = client.post(
                "/api/rules/preview", headers=AUTH,
                json={"pattern": "other.com", "instance_id": "prox"},
            )
            assert resp.status_code == 200
            prox = next(i for i in resp.json()["instances"] if i["id"] == "prox")
            assert prox["counted"] is False and prox["reason"] == "timeout"
        finally:
            ws.__exit__(None, None, None)


# --- WARNING 1: preview ACTIVELY requests a snapshot to be able to count ------
def test_preview_requests_snapshot_to_count(tmp_path):
    # prox is connected but has never delivered a snapshot (snapshot_at NULL). The
    # tabs exist ONLY in the snapshot preview requests: if preview did not actively
    # request one, prox would stay uncountable and closures would be 0. Answering the
    # request lets the two singleton tabs collapse to one => closures==1. Reddens if
    # the snapshot request is removed from the refresh path.
    app = create_app(_settings(tmp_path, snapshot_timeout_ms=1500))
    with TestClient(app) as client:
        ws = _connect_unanswered(client, "prox")
        try:
            # Answer the snapshot_request from a daemon thread, run the preview in
            # another, and wait on the PREVIEW (never block on ws.receive in the main
            # thread). If the request is never sent (guard removed), the answerer just
            # blocks harmlessly and preview times out with closures 0 -> the
            # assertions fail cleanly instead of hanging the test.
            holder = {}

            def _do_preview():
                holder["resp"] = client.post(
                    "/api/rules/preview", headers=AUTH,
                    json={"pattern": "grafana.lc", "instance_id": "prox",
                          "singleton": True},
                )

            def _answer():
                req = ws.receive_json()             # preview's snapshot_request
                if req.get("type") == "snapshot_request":
                    ws.send_json(_snapshot(req["id"], [
                        _tab(1, "https://grafana.lc/a"),
                        _tab(2, "https://grafana.lc/b"),
                    ]))

            answerer = threading.Thread(target=_answer, daemon=True)
            previewer = threading.Thread(target=_do_preview, daemon=True)
            answerer.start()
            previewer.start()
            previewer.join(timeout=8)
            assert not previewer.is_alive(), "preview did not return"
            resp = holder["resp"]
            assert resp.status_code == 200
            payload = resp.json()
            prox = next(i for i in payload["instances"] if i["id"] == "prox")
            assert prox["counted"] is True          # the requested snapshot landed
            assert payload["closures"] == 1         # singleton collapse counted
        finally:
            ws.__exit__(None, None, None)


# --- WARNING 2: an uncountable instance forces confirm even at impact 0 -------
def test_not_counted_instance_forces_confirm(tmp_path):
    # A pre-existing rule (non-empty, no boundary crossing) + a disconnected prox
    # holding a would-drain tab. The counted impact is 0 (prox is uncountable), yet a
    # large burst hides behind it, so confirm MUST be required (§8). Reddens if the
    # not-counted condition is dropped from _requires_confirm (would 201 instead).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox", connected=0, snapshot_at=_now_ms())
        _seed_tab(db_path, "prox", 1, "https://random.io/")   # burst hidden behind prox
        _seed_rule(db_path, "example.com", "prox")            # non-empty already

        body = {"pattern": "other.com", "instance_id": "prox"}
        resp = client.post("/api/rules", headers=AUTH, json=body)
        assert resp.status_code == 409                        # gated by not-counted
        payload = resp.json()
        assert payload["impact"] == 0                         # counted impact is zero
        assert {"id": "prox", "reason": "disconnected"} in payload["not_counted"]
        # Nothing written yet.
        assert len(client.get("/api/rules", headers=AUTH).json()["rules"]) == 1
        # With confirm => created (the human accepted the uncountable risk).
        ok = client.post("/api/rules", headers=AUTH,
                         json={**body, "confirm_impact": True})
        assert ok.status_code == 201


# --- SUGGESTION 1: the survivor example names the §8 ladder winner ------------
def test_singleton_survivor_example_follows_ladder(tmp_path):
    # Two singleton tabs at prox. The ladder winner is the MORE recently active tab
    # (smaller ageMs => larger last_active_at), which is the SECOND DB row here — so
    # the naive "first DB row survives" would name the wrong tab. The closed example
    # must name the loser and carry `survivor` = the ladder winner. Reddens if the
    # ladder sort is removed (then the first row would survive and the assertions
    # invert).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, "prox", tabs=[
            _tab(1, "https://grafana.lc/a", title="A", age_ms=5_000_000),  # older loses
            _tab(2, "https://grafana.lc/b", title="B", age_ms=4_000_000),  # newer wins
        ])
        try:
            resp = client.post(
                "/api/rules/preview", headers=AUTH,
                json={"pattern": "grafana.lc", "instance_id": "prox",
                      "singleton": True},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["closures"] == 1
            ex = payload["closure_examples"][0]
            assert ex["reason"] == "singleton_close"
            assert ex["url"].endswith("/a")             # the OLDER tab is closed
            assert ex["survivor"]["url"].endswith("/b")  # the ladder winner survives
            assert ex["survivor"]["tab_id"] == 2
        finally:
            ws.__exit__(None, None, None)


# --- reset endpoint ---------------------------------------------------------
def test_reset_returns_intent(tmp_path):
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        rid = _seed_rule(db_path, "grafana.lc", "prox")
        c = _conn(db_path)
        c.execute("UPDATE rules SET canonical_url='https://grafana.lc/home' WHERE id=?", (rid,))
        c.commit(); c.close()
        resp = client.post(f"/api/rules/{rid}/reset", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["canonical_url"] == "https://grafana.lc/home"
        assert client.post("/api/rules/999/reset", headers=AUTH).status_code == 404
