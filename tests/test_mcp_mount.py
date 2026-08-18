"""The MCP mount (§11): the app BOOTS with /mcp wired in (no "Task group is not
initialized"), the endpoint is reachable at /mcp (NOT /mcp/mcp), and it is behind the
ADMIN_TOKEN Bearer (issue #35 §4: the agent equals the human — ADMIN_TOKEN, not a
per-instance secret or any other token).

These redden if the §11 mount trap is reintroduced:
* remove ``async with mcp.session_manager.run()`` from the lifespan -> the initialize
  POST 500s with "Task group is not initialized".
* mount the sub-app under /mcp instead of the exact Route -> /mcp doubles to /mcp/mcp.
* drop the Bearer check in the ASGI wrapper -> the no-token request stops being 401.
"""

import json
from types import SimpleNamespace

from conftest import make_settings
from starlette.testclient import TestClient

from src.app import create_app

ADMIN_TOKEN = "test-admin-token"
METRICS_TOKEN = "test-metrics-token"  # a valid OTHER-service token — must NOT open /mcp.
_ACCEPT = "application/json, text/event-stream"


def _settings(tmp_path, **over):
    """This file's settings, built on the ONE shared surface in ``tests/conftest.py``.

    Only what this file deliberately differs on is listed below; everything else — and
    every field ``src.settings.Settings`` grows later — is inherited, so a missing
    attribute can no longer surface as an AttributeError inside an unrelated background
    curator pass (which a TestClient's real lifespan does start).
    """
    return make_settings(tmp_path, **{**{
            "cmd_timeout_ms": 1000,
            "pass_interval_min": 5,
            "snapshot_timeout_ms": 200,
            "state_fresh_ms": 3000,
        }, **over})


def _init_body():
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }


def _headers(token=ADMIN_TOKEN):
    h = {"Accept": _ACCEPT, "Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return h


def _sse_json(text):
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def test_app_boots_and_mcp_initialize_reaches_at_slash_mcp(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # The unrelated routes still work (the app booted normally).
        assert client.get("/healthz").status_code == 200
        # initialize succeeds => the session manager's task group WAS initialized by
        # the host lifespan (no "Task group is not initialized"), reachable at /mcp.
        r = client.post("/mcp", json=_init_body(), headers=_headers())
        assert r.status_code == 200, r.text
        assert r.headers.get("mcp-session-id")


def test_mcp_path_is_not_doubled(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # The sub-app already registers /mcp; a naive mount under /mcp would answer
        # here. It must be a 404 — the endpoint lives at /mcp only.
        r = client.post("/mcp/mcp", json=_init_body(), headers=_headers())
        assert r.status_code == 404


def test_mcp_requires_bearer(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/mcp", json=_init_body(), headers=_headers(token=None)).status_code == 401
        assert client.post("/mcp", json=_init_body(), headers=_headers(token="wrong")).status_code == 401
        # issue #35 §4: only ADMIN_TOKEN opens /mcp — a valid METRICS_TOKEN must not.
        assert client.post(
            "/mcp", json=_init_body(), headers=_headers(token=METRICS_TOKEN)
        ).status_code == 401


def test_mcp_tools_list_exposes_every_phase11_tool(tmp_path):
    app = create_app(_settings(tmp_path))
    expected = {
        "list_instances", "list_tabs", "get_rules", "list_actions",
        "upsert_rule", "delete_rule", "open_tab", "close_tab", "focus_tab",
        "relocate_tab", "reset_singleton", "merge_windows", "execute_js",
        "pause", "resume", "run_pass",
        # The agent-facing observation verbs and the «не трогать» lease. Pinned HERE and
        # not only in test_mcp_tools.py because that file calls the handlers directly: a
        # tool whose signature the MCP SDK cannot serialize would still pass there and be
        # invisible to every agent.
        "get_text", "wait_for", "navigate_tab",
        # wake_tab (#68): un-discards a tab so the injecting verbs stop answering tab_discarded.
        "wake_tab",
        # The fixed-but-mutating write verb: a fixed body (no checkbox / audit) that is still
        # pause-gated on the service side, like navigate_tab.
        "set_input",
        "list_exemptions", "set_exemption", "clear_exemption",
        # The long-running agent verbs (wave 19): the scroll scheduler and the Job-API pair.
        "scroll_until", "start_js", "poll_job",
        # The chrome.debugger foundation (wave 18): the first CDP verb.
        "set_focus_emulation",
        # The WebSocket-capture trio (wave 21): the first DATA-BEARING CDP verbs.
        "start_ws_capture", "read_ws_frames", "stop_ws_capture",
    }
    with TestClient(app) as client:
        r = client.post("/mcp", json=_init_body(), headers=_headers())
        sid = r.headers["mcp-session-id"]
        h = _headers()
        h["mcp-session-id"] = sid
        client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
        assert r.status_code == 200
        data = _sse_json(r.text)
        names = {t["name"] for t in data["result"]["tools"]}
        assert expected <= names
