"""The MCP mount (§11): the app BOOTS with /mcp wired in (no "Task group is not
initialized"), the endpoint is reachable at /mcp (NOT /mcp/mcp), and it is behind the
same EXT_TOKEN Bearer as /api/*.

These redden if the §11 mount trap is reintroduced:
* remove ``async with mcp.session_manager.run()`` from the lifespan -> the initialize
  POST 500s with "Task group is not initialized".
* mount the sub-app under /mcp instead of the exact Route -> /mcp doubles to /mcp/mcp.
* drop the Bearer check in the ASGI wrapper -> the no-token request stops being 401.
"""

import json
from types import SimpleNamespace

from starlette.testclient import TestClient

from src.app import create_app

EXT_TOKEN = "test-ext-token"
_ACCEPT = "application/json, text/event-stream"


def _settings(tmp_path, **over):
    s = dict(
        db_path=str(tmp_path / "curator.db"),
        backup_dir=str(tmp_path / "backups"),
        host="0.0.0.0", port=8000,
        heartbeat_ms=600_000, protocol_version=1,
        ext_token=EXT_TOKEN, metrics_token="m", ext_allowed_origins="",
        idle_minutes=60, pass_interval_min=5, tick_ms=60000,
        cmd_timeout_ms=1000, snapshot_timeout_ms=200, lease_ttl_ms=600_000,
        restore_exemption_min=120, pause_default_min=60, incomplete_after_min=15,
        self_nav_limit=10, state_fresh_ms=3000, quarantine_ttl_min=1440,
        actions_retention_days=90, js_audit_retention_days=730,
        main_instance_id="main", log_level="INFO",
    )
    s.update(over)
    return SimpleNamespace(**s)


def _init_body():
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }


def _headers(token=EXT_TOKEN):
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


def test_mcp_tools_list_exposes_every_phase11_tool(tmp_path):
    app = create_app(_settings(tmp_path))
    expected = {
        "list_instances", "list_tabs", "get_rules", "list_actions",
        "upsert_rule", "delete_rule", "open_tab", "close_tab", "focus_tab",
        "relocate_tab", "reset_singleton", "merge_windows", "execute_js",
        "pause", "resume", "run_pass",
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
