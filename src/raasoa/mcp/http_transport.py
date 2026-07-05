"""Remote MCP transport over HTTP (MCP "Streamable HTTP").

Exposes the same 17 RAASOA MCP tools as the stdio server, but over a
single HTTP endpoint so cloud clients can connect:

  - Claude.ai  (Settings → Connectors → Custom connector)
  - LangDock   (MCP server integration)
  - Microsoft Copilot Studio (MCP tool)

Transport contract (subset of the MCP Streamable HTTP spec that these
clients use):

  POST /mcp   JSON-RPC request  → application/json JSON-RPC response
              JSON-RPC notification (no id) → 202 Accepted, empty body
  GET  /mcp   → 405 (we don't offer server-initiated SSE streams)

Auth: a Bearer token in the Authorization header is forwarded verbatim
to the REST API for every downstream tool call (per-request, via a
ContextVar, so concurrent clients stay isolated). When AUTH_ENABLED is
on, the token is required.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from raasoa.config import settings
from raasoa.mcp import server as mcp_server

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


@router.get("/mcp")
async def mcp_get() -> Response:
    """We don't offer server-initiated SSE streams — POST only."""
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request) -> Response:
    """Handle one JSON-RPC message (request or notification)."""
    # Point the tool layer at the co-located REST API for this process.
    mcp_server.BASE_URL = settings.mcp_internal_url

    token = _extract_bearer(request)
    if settings.auth_enabled and not token:
        return JSONResponse(
            status_code=401,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32001, "message": "Missing bearer token"},
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        msg = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
        )

    if not isinstance(msg, dict):
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid request"},
            },
        )

    # Isolate the per-request bearer for downstream tool calls.
    tok = mcp_server._request_api_key.set(token)
    try:
        response = await mcp_server.handle_message_async(msg)
    finally:
        mcp_server._request_api_key.reset(tok)

    # Notifications (no id / no response) → 202 Accepted, empty body.
    if response is None:
        return Response(status_code=202)
    return JSONResponse(content=response)
