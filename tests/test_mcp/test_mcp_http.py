"""Tests for the remote MCP HTTP transport (/mcp).

Uses FastAPI's TestClient against the real app — no model or DB needed
for the deterministic JSON-RPC paths (initialize, tools/list, auth,
unknown method). tools/call is exercised for response *shape* only.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Ensure the transport is enabled and auth is off for the deterministic
# paths (auth is covered explicitly in its own test via monkeypatch).
os.environ.setdefault("MCP_HTTP_ENABLED", "true")
os.environ.setdefault("AUTH_ENABLED", "false")

from raasoa.main import app  # noqa: E402

client = TestClient(app)


def _rpc(method: str, params: dict | None = None, msg_id: int | None = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        body["id"] = msg_id
    return body


def test_initialize_returns_server_info() -> None:
    r = client.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }))
    assert r.status_code == 200
    data = r.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    assert data["result"]["serverInfo"]["name"] == "raasoa"
    assert "capabilities" in data["result"]


def test_tools_list_over_http() -> None:
    r = client.post("/mcp", json=_rpc("tools/list"))
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"raasoa_search", "raasoa_get_skill", "raasoa_doc_diff"} <= names
    # Policy-gate param must survive over HTTP too
    search = next(t for t in tools if t["name"] == "raasoa_search")
    assert "agent_clearance" in search["inputSchema"]["properties"]


def test_notification_returns_202() -> None:
    # No id => notification => 202 Accepted, empty body
    r = client.post("/mcp", json=_rpc("notifications/initialized", msg_id=None))
    assert r.status_code == 202
    assert r.content in (b"", b"null")


def test_unknown_method_returns_jsonrpc_error() -> None:
    r = client.post("/mcp", json=_rpc("no/such/method"))
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == -32601


def test_get_is_405() -> None:
    r = client.get("/mcp")
    assert r.status_code == 405
    assert r.headers.get("allow") == "POST"


def test_parse_error_on_bad_body() -> None:
    r = client.post("/mcp", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


def test_tools_call_shape_when_api_unreachable() -> None:
    # mcp_internal_url points at :8000 which isn't served under TestClient,
    # so the tool call should degrade to a well-formed isError result, not crash.
    r = client.post("/mcp", json=_rpc("tools/call", {
        "name": "raasoa_search", "arguments": {"query": "x", "top_k": 1},
    }))
    assert r.status_code == 200
    result = r.json()["result"]
    assert "content" in result and isinstance(result["content"], list)


def test_auth_required_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from raasoa.config import settings
    monkeypatch.setattr(settings, "auth_enabled", True)
    # No Authorization header → 401
    r = client.post("/mcp", json=_rpc("tools/list"))
    assert r.status_code == 401
    # With a bearer token → allowed through (tools/list needs no downstream call)
    r2 = client.post("/mcp", json=_rpc("tools/list"),
                     headers={"Authorization": "Bearer sk-test-key"})
    assert r2.status_code == 200
    assert "result" in r2.json()
