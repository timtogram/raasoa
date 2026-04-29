"""End-to-end test: spawn the real MCP server as a stdio subprocess and
talk JSON-RPC to it like Claude Desktop / Cursor would.

This is the only test that exercises the *actual* MCP entry point
(`python -m raasoa.mcp.server`). All other tests call ``_handle_tool_call``
in-process, which would silently break if the stdio loop ever regressed.

Requires a running RAASOA API (set RAASOA_URL, default http://127.0.0.1:8000).
The whole module is auto-skipped when the API isn't reachable, so this
test stays passive in CI/dev shells where the user hasn't started uvicorn.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

API_URL = os.environ.get("RAASOA_URL", "http://127.0.0.1:8000")


def _api_reachable() -> bool:
    try:
        host_port = API_URL.replace("http://", "").replace("https://", "")
        host, port = host_port.split(":", 1)
        port_i = int(port.split("/", 1)[0])
        with socket.create_connection((host, port_i), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _api_reachable(),
    reason=f"RAASOA API not reachable at {API_URL}",
)


class MCPProcess:
    """Tiny helper around the MCP stdio server."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        full_env = os.environ.copy()
        full_env["RAASOA_URL"] = API_URL
        if env:
            full_env.update(env)
        # Run via the same uv venv to ensure asyncpg, httpx, etc. are present
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "raasoa.mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=full_env,
        )
        self._next_id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        msg = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params or {},
        }
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

        # Block on response with a short timeout via stdout polling
        assert self.proc.stdout is not None
        deadline = time.time() + 30  # plenty for first call (cold http client)
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == msg_id:
                return resp
        raise TimeoutError(f"No response for {method} within 30s")

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def mcp() -> MCPProcess:
    p = MCPProcess()
    yield p
    p.close()


def test_initialize(mcp: MCPProcess) -> None:
    """The server announces capabilities on initialize."""
    resp = mcp.send("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "raasoa-stdio-test", "version": "1.0"},
    })
    assert "result" in resp, resp
    result = resp["result"]
    assert "capabilities" in result
    # MCP requires a serverInfo block
    assert "serverInfo" in result
    assert result["serverInfo"]["name"]


def test_tools_list_includes_skill_tool(mcp: MCPProcess) -> None:
    """All registered tools come back over stdio, including the new ones."""
    mcp.send("initialize", {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"}})
    mcp.notify("notifications/initialized")
    resp = mcp.send("tools/list")
    assert "result" in resp
    tools = resp["result"].get("tools", [])
    names = {t["name"] for t in tools}
    # Spot-check a handful of critical tools across our recent feature work
    expected = {
        "raasoa_search",
        "raasoa_get_skill",
        "raasoa_doc_diff",
        "raasoa_doc_dependencies",
        "raasoa_find_by_metadata",
    }
    missing = expected - names
    assert not missing, f"Missing tools over stdio: {missing}"

    # Verify policy-gate parameter survived JSON-RPC
    search = next(t for t in tools if t["name"] == "raasoa_search")
    props = search["inputSchema"]["properties"]
    assert "agent_clearance" in props, (
        "agent_clearance missing — Policy-Gate would not be exposed to clients"
    )


def test_tools_call_search_round_trip(mcp: MCPProcess) -> None:
    """End-to-end: stdio client calls raasoa_search → MCP → HTTP API → result."""
    mcp.send("initialize", {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"}})
    mcp.notify("notifications/initialized")
    resp = mcp.send("tools/call", {
        "name": "raasoa_search",
        "arguments": {"query": "anything", "top_k": 1},
    })
    # Either content (success) or an error block — but never a malformed reply
    assert "result" in resp or "error" in resp, resp
    if "result" in resp:
        result = resp["result"]
        # MCP tool responses must always have a content list
        assert "content" in result, result
        assert isinstance(result["content"], list)


def test_unknown_method_returns_error(mcp: MCPProcess) -> None:
    """The server politely refuses unsupported methods instead of crashing."""
    mcp.send("initialize", {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"}})
    resp = mcp.send("definitely/not/a/real/method", {})
    # MCP servers respond with a JSON-RPC error for unknown methods
    assert "error" in resp, resp
    assert resp["error"]["code"] != 0
