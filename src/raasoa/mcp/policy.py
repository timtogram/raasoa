"""MCP Policy-Gate.

Filters retrieval results before they leave the MCP boundary based on
per-tenant data-classification policies.

Each document can declare a sensitivity level via frontmatter
``classification`` (e.g. ``public``, ``internal``, ``confidential``).
The MCP caller passes an ``agent_clearance`` argument; results that
exceed the clearance are dropped and the denial is audit-logged.

Default policy (no per-tenant override): ``public``-only.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Lower number = more sensitive (higher clearance required)
CLASSIFICATION_RANK: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "restricted": 2,
    "confidential": 3,
    "secret": 4,
}

DEFAULT_CLEARANCE = "public"


def _rank(level: str | None) -> int:
    if not level:
        return 0  # missing classification = treat as public
    return CLASSIFICATION_RANK.get(level.strip().lower(), 0)


def hit_is_allowed(hit: dict[str, Any], clearance: str) -> bool:
    """Decide whether a single retrieval hit may be returned."""
    classification = (
        (hit.get("metadata") or {}).get("classification")
        or (hit.get("doc_metadata") or {}).get("classification")
        or hit.get("classification")
    )
    return _rank(classification) <= _rank(clearance)


def apply_policy_gate(
    hits: list[dict[str, Any]],
    *,
    clearance: str = DEFAULT_CLEARANCE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter hits by policy. Returns ``(allowed, denied)``.

    Each denied hit carries ``policy_reason`` for audit logging.
    """
    allowed: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    for h in hits:
        if hit_is_allowed(h, clearance):
            allowed.append(h)
        else:
            classification = (
                (h.get("metadata") or {}).get("classification")
                or h.get("classification")
                or "unknown"
            )
            d = dict(h)
            d["policy_reason"] = (
                f"classification={classification} exceeds clearance={clearance}"
            )
            denied.append(d)
    return allowed, denied


async def audit_denials(
    base_url: str,
    headers: dict[str, str],
    *,
    tool: str,
    query: str | None,
    denied: list[dict[str, Any]],
) -> None:
    """Best-effort audit log for denied items.

    Posts to ``/v1/analytics/audit`` (the endpoint's own docstring notes
    it's "Used by MCP policy-gate"); falls back to a structured logger
    line so denials are still observable when the audit call fails.
    """
    if not denied:
        return
    reasons = [d.get("policy_reason") for d in denied]
    doc_ids = [
        d.get("document_id") or d.get("doc_id") or d.get("id") for d in denied
    ]
    payload: dict[str, Any] = {
        "action": "mcp.policy_denied",
        "resource_type": "tool_call",
        "resource_id": tool,
        "details": {
            "tool": tool,
            "query": query,
            "denied_count": len(denied),
            "reasons": reasons,
            "doc_ids": doc_ids,
        },
    }
    # Try the audit endpoint, but never block the tool response on it
    try:
        timeout = httpx.Timeout(5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url}/v1/analytics/audit",
                json=payload,
                headers=headers,
            )
            if r.status_code >= 400:
                logger.warning(
                    "policy_denied audit returned %s: %s",
                    r.status_code, r.text[:200],
                )
    except Exception as e:
        logger.warning("policy_denied audit failed: %s", e)

    # Always emit a structured log line
    logger.info(
        "POLICY_DENIED tool=%s query=%r denied=%d reasons=%s",
        tool, query, len(denied), reasons,
    )


def env_default_clearance() -> str:
    """Server-wide default clearance for MCP — used when a tool call
    doesn't specify ``agent_clearance``."""
    return os.environ.get("RAASOA_MCP_DEFAULT_CLEARANCE", DEFAULT_CLEARANCE)


def effective_clearance(requested: str | None) -> str:
    """Resolve the clearance actually applied to a tool call.

    The env-configured default (``RAASOA_MCP_DEFAULT_CLEARANCE``) is a
    hard ceiling: an agent may request a *lower* clearance than the
    ceiling (to intentionally see less), but a request for a *higher*
    clearance is clamped down to the ceiling, never honored as-is.
    Previously ``arguments.get("agent_clearance") or env_default_clearance()``
    let any caller-supplied value override the ceiling outright — a
    caller passing ``agent_clearance: "secret"`` saw everything
    regardless of the server-side default.
    """
    ceiling = env_default_clearance()
    if not requested:
        return ceiling
    return requested if _rank(requested) <= _rank(ceiling) else ceiling
