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
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from conftest import _recv, approve_instance, make_settings, secret_hash_for
from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
AUTH = {"Authorization": f"Bearer {EXT_TOKEN}"}

IDLE_MS = 60 * 60_000            # idle_minutes=60 => a tab must be ~1h idle to move
OLD = 4_000_000                  # an age (ms) comfortably past IDLE_MS => guarded


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
            "state_fresh_ms": 3000,
        }, **over})


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


def _set_setting(db_path, key, value):
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
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
        "secretHash": secret_hash_for(instance_id),
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
    # Secret-based hello (issue #35): approve the instance (Task E) before it can hello.
    approve_instance(db_path, instance_id)
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    _recv(ws)                # hello_ack
    req = _recv(ws)          # snapshot_request
    ws.send_json(_snapshot(req["id"], tabs or [], session=session))
    # HARD assert, not a best-effort wait: the channel clears ``pending_snapshot_id``
    # BEFORE it writes the snapshot, so "snapshot_at is set" is the proof that the
    # request slot is free again. Letting an unlanded handshake slide made every later
    # step race — ``/api/state``'s kick correctly SKIPS an instance whose slot is still
    # occupied ("a refresh is already in flight"), and the test would then wait forever
    # for a frame that was never going to be sent.
    assert _wait_until(
        lambda: _db_row(
            db_path, "SELECT snapshot_at FROM instances WHERE id=?", (instance_id,)
        )[0]
        is not None
    ), f"instance {instance_id!r} never applied its initial snapshot"
    return ws


def _connect_unanswered(client, db_path, instance_id, session="sess-1"):
    """hello + DISCARD the initial snapshot_request (leave snapshot_at NULL).

    The instance is connected but has never delivered a snapshot, so preview MUST
    actively request one — the hook the `preview-requests-snapshot` guard tests.
    """
    # Secret-based hello (issue #35): approve the instance (Task E) before it can hello.
    approve_instance(db_path, instance_id)
    ws = client.websocket_connect("/ext").__enter__()
    ws.send_json(_hello(instance_id=instance_id, session=session))
    _recv(ws)                # hello_ack
    _recv(ws)                # initial snapshot_request (discarded)
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
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_unanswered(client, db_path, "prox")  # connected, snapshot_at NULL
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
    # prox is connected with a STALE mirror and a free request slot. The tabs exist only
    # in the snapshot preview asks for: if preview did not actively request one, prox
    # would count against the stale mirror and closures would be 0. Answering the request
    # lets the two singleton tabs collapse to one => closures==1. Reddens if the snapshot
    # request is removed from the refresh path.
    #
    # The mirror is aged by REWRITING snapshot_at, and the handshake IS answered, so the
    # slot is provably free: this test is about "preview asks", not about how long it
    # waits for somebody else's in-flight request (that is
    # test_preview_waits_for_a_pass_snapshot_instead_of_clobbering_it).
    app = create_app(_settings(tmp_path, snapshot_timeout_ms=1500, state_fresh_ms=3000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, "prox", tabs=[])
        c = _conn(db_path)
        c.execute("UPDATE instances SET snapshot_at = 0 WHERE id='prox'")
        c.commit()
        c.close()
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
                req = _recv(ws)             # preview's snapshot_request
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


# --- GET /api/rules/:id -----------------------------------------------------
def test_get_one_rule_matches_the_list_element(tmp_path):
    # §10's `GET /api/rules[/:id]`. The single-rule body must be byte-for-byte the
    # object the list puts in `rules[]` — reddens if the two ever grow separate mappers.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        rid = _seed_rule(db_path, "grafana.lc", "prox", singleton=1)
        listed = client.get("/api/rules", headers=AUTH).json()["rules"]
        one = client.get(f"/api/rules/{rid}", headers=AUTH)
        assert one.status_code == 200
        assert one.json() == next(r for r in listed if r["id"] == rid)
        assert client.get("/api/rules/999", headers=AUTH).status_code == 404
        assert client.get(f"/api/rules/{rid}").status_code == 401


# --- reset: the ONE thing that changes tab content (§8, §10) ----------------
def _set_canonical(db_path, rule_id, url):
    c = _conn(db_path)
    try:
        c.execute("UPDATE rules SET canonical_url=? WHERE id=?", (url, rule_id))
        c.commit()
    finally:
        c.close()


def test_reset_navigates_survivor_and_journals_it(tmp_path):
    # The real manual reset: the SURVIVING tab of the rule (§8 ladder — tab 2 is the
    # more recently active) is navigated to canonical_url and an actions(kind='reset')
    # row lands. Reddens to a hang/500 if the navigate_tab command is not sent, and to
    # a missing row if the archive write is dropped.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    now = _now_ms()
    with TestClient(app) as client:
        rid = _seed_rule(db_path, "grafana.lc", "prox", singleton=1)
        _set_canonical(db_path, rid, "https://grafana.lc/home")
        ws = _connect_fresh(
            client, db_path, "prox",
            tabs=[
                _tab(1, "https://grafana.lc/a", age_ms=OLD * 2),   # older -> loses
                _tab(2, "https://grafana.lc/b", age_ms=OLD),       # survivor
            ],
        )
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(lambda: client.post(f"/api/rules/{rid}/reset", headers=AUTH))
            cmd = _recv(ws)
            assert cmd["type"] == "command"
            assert cmd["command"] == "navigate_tab"
            assert cmd["sessionId"] == "sess-1"            # §5 session stamped
            assert cmd["params"] == {"tabId": 2, "url": "https://grafana.lc/home"}
            ws.send_json({"type": "response", "id": cmd["id"], "ok": True, "result": {}})
            resp = fut.result(timeout=5)
            assert resp.status_code == 200
            body = resp.json()
            assert body["reset"] is True and body["tab_id"] == 2
            assert body["canonical_url"] == "https://grafana.lc/home"

            row = _db_row(
                db_path,
                "SELECT kind, status, initiator, instance_from, tab_id, rule_id, url, "
                "detail FROM actions WHERE kind='reset'",
            )
            assert row == ("reset", "done", "user", "prox", 2, rid,
                           "https://grafana.lc/home", "https://grafana.lc/b")
            assert _db_row(db_path, "SELECT COUNT(*) FROM actions") == (1,)
            assert row[0] and now  # the row is the pass-free manual action (no pass_id)
            assert _db_row(
                db_path, "SELECT pass_id FROM actions WHERE kind='reset'"
            ) == (None,)
        finally:
            ws.__exit__(None, None, None)


def test_reset_with_no_tabs_is_not_an_error(tmp_path):
    # §8: a rule that currently holds no tab has nothing to reset. That is a 200 with
    # reset=false, NOT an error and NOT a command — reddens if the no-tab branch starts
    # sending navigate_tab (the ws would receive a frame and the call would hang out).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        rid = _seed_rule(db_path, "grafana.lc", "prox")
        _set_canonical(db_path, rid, "https://grafana.lc/home")
        ws = _connect_fresh(client, db_path, "prox", tabs=[_tab(1, "https://other.io/x")])
        try:
            resp = client.post(f"/api/rules/{rid}/reset", headers=AUTH)
            assert resp.status_code == 200
            assert resp.json()["reset"] is False
            assert resp.json()["reason"] == "no_tabs"
            assert _db_row(db_path, "SELECT COUNT(*) FROM actions") == (0,)
        finally:
            ws.__exit__(None, None, None)


def test_reset_refuses_disconnected_instance_and_missing_rule(tmp_path):
    # An unreachable instance cannot be navigated: 409 like every other command path,
    # never a silent success. A rule with no canonical_url has no target: 422.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox", connected=0)
        rid = _seed_rule(db_path, "grafana.lc", "prox")
        _set_canonical(db_path, rid, "https://grafana.lc/home")
        assert client.post(f"/api/rules/{rid}/reset", headers=AUTH).status_code == 409

        bare = _seed_rule(db_path, "other.io", "prox")          # no canonical_url
        assert client.post(f"/api/rules/{bare}/reset", headers=AUTH).status_code == 422
        assert client.post("/api/rules/999/reset", headers=AUTH).status_code == 404
        assert _db_row(db_path, "SELECT COUNT(*) FROM actions") == (0,)


def test_reset_refused_while_paused(tmp_path):
    # §7: a pause silences the mutating verbs, and reset is one — no force here (it is
    # not one of the three human buttons §7 names).
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        rid = _seed_rule(db_path, "grafana.lc", "prox")
        _set_canonical(db_path, rid, "https://grafana.lc/home")
        _set_setting(db_path, "pause_until", str(_now_ms() + 3_600_000))
        resp = client.post(f"/api/rules/{rid}/reset", headers=AUTH)
        assert resp.status_code == 423
        assert resp.json()["error"] == "paused"
        # force is NOT honoured for reset.
        forced = client.post(
            f"/api/rules/{rid}/reset", headers=AUTH, json={"force": True}
        )
        assert forced.status_code == 423
        assert _db_row(db_path, "SELECT COUNT(*) FROM actions") == (0,)


def test_reset_failure_is_journaled(tmp_path):
    # The extension refuses (no_such_tab: the mirror named a tab that is gone). The
    # attempt must still land in the archive as status='failed' with the §6 code —
    # reddens if the failure path skips the row.
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        rid = _seed_rule(db_path, "grafana.lc", "prox")
        _set_canonical(db_path, rid, "https://grafana.lc/home")
        ws = _connect_fresh(
            client, db_path, "prox", tabs=[_tab(1, "https://grafana.lc/a")]
        )
        try:
            pool = ThreadPoolExecutor(1)
            fut = pool.submit(lambda: client.post(f"/api/rules/{rid}/reset", headers=AUTH))
            cmd = _recv(ws)
            ws.send_json({
                "type": "response", "id": cmd["id"], "ok": False,
                "error": {"code": "no_such_tab", "message": "gone"},
            })
            resp = fut.result(timeout=5)
            assert resp.status_code == 409
            assert resp.json()["error"] == "no_such_tab"
            assert _db_row(
                db_path, "SELECT kind, status, reason FROM actions"
            ) == ("reset", "failed", "no_such_tab")
        finally:
            ws.__exit__(None, None, None)


# --- preview never ejects an instance from a running pass -------------------
def test_preview_waits_for_a_pass_snapshot_instead_of_clobbering_it(tmp_path):
    """A preview opened WHILE a curator pass is collecting snapshots must not overwrite
    the pass's ``pending_snapshot_id``: the channel matches ids exactly, so the
    instance's answer to the pass would be dropped and it would be silently excluded
    from that pass (§7). Preview waits for the pass's snapshot instead — the same
    mirror it wanted anyway.

    Reddens if the guard is removed: the slot becomes a ``req-`` id at the mid-flight
    assertion, and the ``pass-abc`` snapshot below is then rejected by the channel, so
    prox never becomes countable and the closure count collapses to 0."""
    app = create_app(_settings(tmp_path, state_fresh_ms=5_000, snapshot_timeout_ms=4_000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        ws = _connect_fresh(client, db_path, "prox", tabs=[])
        try:
            # Make the mirror stale so preview has to freshen it.
            c = _conn(db_path)
            c.execute("UPDATE instances SET snapshot_at = 0 WHERE id='prox'")
            c.commit(); c.close()

            cs = client.app.state.ext_registry.get("prox")
            cs.pending_snapshot_id = "pass-abc"        # a pass is awaiting THIS id
            cs.pending_sent_at = _now_ms()

            holder = {}

            def _do_preview():
                holder["resp"] = client.post(
                    "/api/rules/preview", headers=AUTH,
                    json={"pattern": "grafana.lc", "instance_id": "prox",
                          "singleton": True},
                )

            previewer = threading.Thread(target=_do_preview, daemon=True)
            previewer.start()
            time.sleep(0.4)
            assert cs.pending_snapshot_id == "pass-abc", "preview clobbered the pass slot"

            # The instance answers the PASS's id: it stays in the pass, and preview gets
            # the fresh mirror it was waiting for.
            ws.send_json(_snapshot("pass-abc", [
                _tab(1, "https://grafana.lc/a"),
                _tab(2, "https://grafana.lc/b"),
            ], session="sess-1"))
            previewer.join(timeout=8)
            assert not previewer.is_alive(), "preview did not return"
            payload = holder["resp"].json()
            prox = next(i for i in payload["instances"] if i["id"] == "prox")
            assert prox["counted"] is True
            assert payload["closures"] == 1            # counted off the pass's snapshot
            assert cs.last_applied_snapshot_id == "pass-abc"   # still in the pass
        finally:
            ws.__exit__(None, None, None)


# --- preview freshens instances CONCURRENTLY, not one after another ---------
def test_preview_refreshes_instances_in_parallel(tmp_path):
    """Two unresponsive instances must cost ONE budget, not two.

    As a sequential loop the per-instance waits added up, so the wall time of an
    interactive rule edit grew with the size of the fleet — exactly backwards. Reddens
    (roughly doubles) if the ``asyncio.gather`` in ``_refresh_for_preview`` goes back to
    a comprehension with an ``await`` inside.
    """
    app = create_app(_settings(tmp_path, snapshot_timeout_ms=700, state_fresh_ms=3000))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        # Two connected instances whose mirrors are stale and which never answer.
        sockets = [
            _connect_fresh(client, db_path, iid, session=sess, tabs=[])
            for iid, sess in (("prox", "s-prox"), ("media", "s-media"))
        ]
        try:
            c = _conn(db_path)
            c.execute("UPDATE instances SET snapshot_at = 0")
            c.commit()
            c.close()

            started = time.time()
            resp = client.post(
                "/api/rules/preview", headers=AUTH,
                json={"pattern": "other.com", "instance_id": "prox"},
            )
            elapsed = time.time() - started

            assert resp.status_code == 200
            reasons = {i["id"]: i["reason"] for i in resp.json()["instances"]}
            assert reasons["prox"] == "timeout" and reasons["media"] == "timeout"
            # One budget (0.7s) plus slack — NOT the ~1.4s a sequential fan-out costs.
            assert elapsed < 1.2, f"instances were refreshed sequentially ({elapsed:.2f}s)"
        finally:
            # An unclosed websocket wedges TestClient.__exit__ (the portal waits for it),
            # which turns a failure here into a hang instead of a red test.
            for ws in sockets:
                ws.__exit__(None, None, None)


# --- canonical_url is validated at SAVE, not only at the extension edge -----
def test_canonical_url_is_validated_on_save(tmp_path):
    """§12 wants the edge check «а не только» rule validation — there was no server-side
    one. Since the manual reset really sends this value in a ``navigate_tab``, an
    unvalidated one fails at the far end: the extension refuses it, the human gets a bare
    ``precondition_failed`` and an ``actions`` row saying ``failed``, and nothing says
    "the URL you typed is not a URL"."""
    app = create_app(_settings(tmp_path))
    db_path = str(tmp_path / "curator.db")
    with TestClient(app) as client:
        _seed_instance(db_path, "prox")
        for bad in ("data:text/html,x", "javascript:alert(1)", "grafana.lc/home",
                    "file:///etc/passwd", "https://", 42):
            r = client.post(
                "/api/rules", headers=AUTH,
                json={"pattern": "grafana.lc", "instance_id": "prox",
                      "canonical_url": bad, "confirm_impact": True},
            )
            assert r.status_code == 422, f"{bad!r} was accepted"
        assert client.get("/api/rules", headers=AUTH).json()["rules"] == []

        # Absent/empty stays legal — canonical_url is optional (a rule without one just
        # has no reset target).
        ok = client.post(
            "/api/rules", headers=AUTH,
            json={"pattern": "grafana.lc", "instance_id": "prox", "confirm_impact": True},
        )
        assert ok.status_code == 201
        # …and a real URL is accepted.
        rid = ok.json()["id"]
        assert client.put(
            f"/api/rules/{rid}", headers=AUTH,
            json={"pattern": "grafana.lc", "instance_id": "prox",
                  "canonical_url": "https://grafana.lc/home", "confirm_impact": True},
        ).status_code == 200
