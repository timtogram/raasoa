"""Analytics endpoints for quality visibility and contradiction hotspots.

Provides aggregated views across sources, documents, and claims
to answer: "Where is our knowledge most unstable?"
"""


from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


@router.get("/audit")
async def audit_log(
    request: Request,
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Query the audit log. Filterable by action and resource type."""
    tenant_id = resolve_tenant(request)

    conditions = ["tenant_id = :tid"]
    params: dict[str, Any] = {"tid": tenant_id, "lim": limit}

    if action:
        conditions.append("action = :action")
        params["action"] = action
    if resource_type:
        conditions.append("resource_type = :rtype")
        params["rtype"] = resource_type

    where = " AND ".join(conditions)
    result = await session.execute(
        text(
            f"SELECT id, actor, action, resource_type, resource_id, "
            f"details, ip_address, created_at "
            f"FROM audit_events WHERE {where} "
            f"ORDER BY created_at DESC LIMIT :lim"
        ),
        params,
    )
    return [
        {
            "id": str(r.id),
            "actor": r.actor,
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "details": r.details,
            "ip_address": r.ip_address,
            "created_at": str(r.created_at),
        }
        for r in result.fetchall()
    ]


@router.get("/quality-by-source")
async def quality_by_source(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Quality scores aggregated per source.

    Shows which data sources produce the best/worst quality content.
    """
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT s.name as source_name, s.source_type, "
            "COUNT(d.id) as document_count, "
            "ROUND(AVG(d.quality_score)::numeric, 3) as avg_quality, "
            "ROUND(MIN(d.quality_score)::numeric, 3) as min_quality, "
            "COUNT(*) FILTER (WHERE d.quality_score < 0.5) "
            "  as low_quality_count, "
            "COUNT(*) FILTER (WHERE d.review_status = 'quarantined') "
            "  as quarantined_count "
            "FROM documents d "
            "JOIN sources s ON d.source_id = s.id "
            "WHERE d.tenant_id = :tid AND d.status != 'deleted' "
            "GROUP BY s.id, s.name, s.source_type "
            "ORDER BY avg_quality ASC NULLS LAST"
        ),
        {"tid": tenant_id},
    )
    return [
        {
            "source_name": r.source_name,
            "source_type": r.source_type,
            "document_count": r.document_count,
            "avg_quality": float(r.avg_quality) if r.avg_quality else None,
            "min_quality": float(r.min_quality) if r.min_quality else None,
            "low_quality_count": r.low_quality_count,
            "quarantined_count": r.quarantined_count,
        }
        for r in result.fetchall()
    ]


@router.get("/contradiction-hotspots")
async def contradiction_hotspots(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Sources and documents with the most contradictions.

    Answers: "Where is our knowledge most unstable?"
    """
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT d.title as document_title, d.id as document_id, "
            "s.name as source_name, "
            "COUNT(cc.id) as conflict_count, "
            "COUNT(*) FILTER (WHERE cc.status = 'new') "
            "  as unresolved_count, "
            "ROUND(AVG(cc.confidence)::numeric, 3) as avg_confidence "
            "FROM conflict_candidates cc "
            "JOIN documents d ON ("
            "  cc.document_a_id = d.id OR cc.document_b_id = d.id"
            ") "
            "JOIN sources s ON d.source_id = s.id "
            "WHERE cc.tenant_id = :tid "
            "GROUP BY d.id, d.title, s.name "
            "ORDER BY unresolved_count DESC, conflict_count DESC "
            "LIMIT 20"
        ),
        {"tid": tenant_id},
    )
    return [
        {
            "document_title": r.document_title,
            "document_id": str(r.document_id),
            "source_name": r.source_name,
            "conflict_count": r.conflict_count,
            "unresolved_count": r.unresolved_count,
            "avg_confidence": float(r.avg_confidence)
            if r.avg_confidence
            else None,
        }
        for r in result.fetchall()
    ]


@router.get("/claim-stability")
async def claim_stability(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Claim stability overview.

    Shows how often claims get superseded — indicates knowledge churn.
    """
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT "
            "COUNT(*) as total_claims, "
            "COUNT(*) FILTER (WHERE c.status = 'active') "
            "  as active_claims, "
            "COUNT(*) FILTER (WHERE c.status = 'superseded') "
            "  as superseded_claims, "
            "COUNT(*) FILTER (WHERE c.status = 'rejected') "
            "  as rejected_claims, "
            "COUNT(DISTINCT c.document_id) as documents_with_claims "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE d.tenant_id = :tid"
        ),
        {"tid": tenant_id},
    )
    row = result.first()
    total = row.total_claims if row else 0
    superseded = row.superseded_claims if row else 0

    return {
        "total_claims": total,
        "active_claims": row.active_claims if row else 0,
        "superseded_claims": superseded,
        "rejected_claims": row.rejected_claims if row else 0,
        "documents_with_claims": row.documents_with_claims if row else 0,
        "stability_rate": round(
            1.0 - (superseded / total) if total > 0 else 1.0, 3
        ),
    }
