"""Usage metering — tracks operations per tenant.

Every ingest, retrieve, LLM call, and embedding call is logged
as a usage_event. This enables:
- Billing (SaaS)
- Quota enforcement
- Usage analytics per tenant

Best-effort: metering failures never block the parent operation.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def track_usage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    event_type: str,
    quantity: int = 1,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a usage event. Best-effort — never fails parent request."""
    try:
        import json

        await session.execute(
            text(
                "INSERT INTO usage_events "
                "(id, tenant_id, event_type, quantity, metadata) "
                "VALUES (:id, :tid, :etype, :qty, CAST(:meta AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "etype": event_type,
                "qty": quantity,
                "meta": json.dumps(metadata or {}),
            },
        )
    except Exception:
        pass  # Best-effort


async def check_quota(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    quota_type: str,
) -> tuple[bool, str]:
    """Check if a tenant is within their quota.

    Returns (allowed, reason).
    """
    try:
        if quota_type == "documents":
            result = await session.execute(
                text(
                    "SELECT "
                    "  (SELECT COALESCE(max_documents, 100) "
                    "   FROM tenants WHERE id = :tid) AS max_docs, "
                    "  (SELECT COUNT(*) FROM documents "
                    "   WHERE tenant_id = :tid "
                    "   AND status != 'deleted') AS current_docs"
                ),
                {"tid": tenant_id},
            )
            row = result.first()
            if row and row.current_docs >= row.max_docs:
                return (
                    False,
                    f"Document limit reached ({row.current_docs}/{row.max_docs}). "
                    "Upgrade your plan.",
                )

        elif quota_type == "queries":
            result = await session.execute(
                text(
                    "SELECT "
                    "  (SELECT COALESCE(max_queries_per_month, 1000) "
                    "   FROM tenants WHERE id = :tid) AS max_queries, "
                    "  (SELECT COUNT(*) FROM usage_events "
                    "   WHERE tenant_id = :tid "
                    "   AND event_type = 'retrieve' "
                    "   AND created_at > date_trunc('month', now())"
                    "  ) AS current_queries"
                ),
                {"tid": tenant_id},
            )
            row = result.first()
            if row and row.current_queries >= row.max_queries:
                return (
                    False,
                    f"Monthly query limit reached "
                    f"({row.current_queries}/{row.max_queries}). "
                    "Upgrade your plan.",
                )

        elif quota_type == "sources":
            result = await session.execute(
                text(
                    "SELECT "
                    "  (SELECT COALESCE(max_sources, 1) "
                    "   FROM tenants WHERE id = :tid) AS max_src, "
                    "  (SELECT COUNT(*) FROM sources "
                    "   WHERE tenant_id = :tid) AS current_src"
                ),
                {"tid": tenant_id},
            )
            row = result.first()
            if row and row.current_src >= row.max_src:
                return (
                    False,
                    f"Source limit reached ({row.current_src}/{row.max_src}). "
                    "Upgrade your plan.",
                )

    except Exception:
        pass  # Quota check is best-effort — allow on failure

    return (True, "ok")
