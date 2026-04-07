"""Audit logging — compliance-grade event tracking.

Every mutation (ingest, delete, resolve, approve, reject, sync)
is logged with: who did it, what resource, when, from where.

Usage:
    await audit(session, tenant_id, request, "document.ingest",
                "document", str(doc.id), {"title": doc.title})
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    request: Request | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Log an audit event. Best-effort — never fails the parent request."""
    try:
        # Determine actor from auth header
        actor = "anonymous"
        if request:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                # Show key prefix only (sk-xxx...yyy)
                key = auth[7:]
                actor = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else key
            elif request.cookies.get("raasoa_session"):
                actor = "dashboard"

        ip_address = None
        if request and request.client:
            ip_address = request.client.host

        await session.execute(
            text(
                "INSERT INTO audit_events "
                "(id, tenant_id, actor, action, resource_type, "
                " resource_id, details, ip_address) "
                "VALUES (:id, :tid, :actor, :action, :rtype, "
                " :rid, CAST(:details AS jsonb), :ip)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "actor": actor,
                "action": action,
                "rtype": resource_type,
                "rid": resource_id,
                "details": __import__("json").dumps(details or {}),
                "ip": ip_address,
            },
        )
    except Exception:
        pass  # Audit is best-effort
