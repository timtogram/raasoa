"""Tests for the MCP server protocol handling."""

from raasoa.mcp.server import (
    _handle_message,
    _make_error,
    _make_response,
    _resource_definitions,
    _tool_definitions,
)


def test_make_response() -> None:
    resp = _make_response(1, {"tools": []})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"] == {"tools": []}


def test_make_error() -> None:
    resp = _make_error(1, -32601, "Method not found")
    assert resp["error"]["code"] == -32601
    assert resp["error"]["message"] == "Method not found"


def test_tool_definitions_not_empty() -> None:
    tools = _tool_definitions()
    assert len(tools) >= 4
    names = [t["name"] for t in tools]
    assert "raasoa_search" in names
    assert "raasoa_ingest" in names
    assert "raasoa_list_documents" in names


def test_resource_definitions_not_empty() -> None:
    resources = _resource_definitions()
    assert len(resources) >= 2
    uris = [r["uri"] for r in resources]
    assert "raasoa://health" in uris
    assert "raasoa://stats" in uris


def test_initialize_handler() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    resp = _handle_message(msg)
    assert resp is not None
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"] == "raasoa"


def test_tools_list_handler() -> None:
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = _handle_message(msg)
    assert resp is not None
    assert len(resp["result"]["tools"]) >= 4


def test_resources_list_handler() -> None:
    msg = {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}
    resp = _handle_message(msg)
    assert resp is not None
    assert len(resp["result"]["resources"]) >= 2


def test_ping_handler() -> None:
    msg = {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}}
    resp = _handle_message(msg)
    assert resp is not None
    assert resp["result"] == {}


def test_unknown_method_returns_error() -> None:
    msg = {"jsonrpc": "2.0", "id": 5, "method": "unknown/method", "params": {}}
    resp = _handle_message(msg)
    assert resp is not None
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_notification_returns_none() -> None:
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    resp = _handle_message(msg)
    assert resp is None


def test_tool_call_with_connection_error() -> None:
    """Tool call when API is not running should return error content."""
    msg = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "raasoa_search",
            "arguments": {"query": "test"},
        },
    }
    resp = _handle_message(msg)
    assert resp is not None
    # Should handle gracefully (either error or content with error flag)
    result = resp.get("result", {})
    content = result.get("content", [])
    assert len(content) > 0
