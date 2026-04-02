"""Structured queries: Answer metadata/aggregation questions via direct SQL.

These bypass the vector/BM25 search and query document metadata directly.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class StructuredResult:
    answer: str
    data: list[dict[str, Any]]
    query_type: str


async def structured_query(
    session: AsyncSession,
    query: str,
    tenant_id: uuid.UUID,
) -> StructuredResult:
    """Execute a structured query against document metadata.

    Supports:
    - Document counts and listings
    - Quality score aggregations
    - Conflict/review status summaries
    """
    query_lower = query.lower()

    # Document count
    if "how many" in query_lower and "document" in query_lower:
        result = await session.execute(
            text(
                "SELECT COUNT(*) as total, "
                "COUNT(*) FILTER (WHERE status = 'indexed') as indexed, "
                "COUNT(*) FILTER (WHERE status = 'quarantined') as quarantined "
                "FROM documents WHERE tenant_id = :tid AND status != 'deleted'"
            ),
            {"tid": tenant_id},
        )
        row = result.first()
        if row is None:
            return StructuredResult(answer="No data", data=[], query_type="document_count")
        answer = (
            f"Total: {row.total} documents "
            f"({row.indexed} indexed, {row.quarantined} quarantined)"
        )
        return StructuredResult(
            answer=answer,
            data=[{"total": row.total, "indexed": row.indexed, "quarantined": row.quarantined}],
            query_type="document_count",
        )

    # Quality overview
    if "quality" in query_lower and ("score" in query_lower or "overview" in query_lower):
        result = await session.execute(
            text(
                "SELECT "
                "ROUND(AVG(quality_score)::numeric, 2) as avg_score, "
                "ROUND(MIN(quality_score)::numeric, 2) as min_score, "
                "ROUND(MAX(quality_score)::numeric, 2) as max_score, "
                "COUNT(*) FILTER (WHERE quality_score >= 0.8) as high_quality, "
                "COUNT(*) FILTER (WHERE quality_score < 0.5) as low_quality "
                "FROM documents WHERE tenant_id = :tid "
                "AND status != 'deleted' AND quality_score IS NOT NULL"
            ),
            {"tid": tenant_id},
        )
        row = result.first()
        if row is None:
            return StructuredResult(answer="No data", data=[], query_type="quality_overview")
        return StructuredResult(
            answer=(
                f"Average quality: {row.avg_score} "
                f"(range: {row.min_score}-{row.max_score}, "
                f"{row.high_quality} high quality, {row.low_quality} low quality)"
            ),
            data=[{
                "avg_score": float(row.avg_score) if row.avg_score else 0,
                "min_score": float(row.min_score) if row.min_score else 0,
                "max_score": float(row.max_score) if row.max_score else 0,
                "high_quality": row.high_quality,
                "low_quality": row.low_quality,
            }],
            query_type="quality_overview",
        )

    # Conflict summary
    if "conflict" in query_lower:
        result = await session.execute(
            text(
                "SELECT status, COUNT(*) as cnt "
                "FROM conflict_candidates WHERE tenant_id = :tid "
                "GROUP BY status ORDER BY cnt DESC"
            ),
            {"tid": tenant_id},
        )
        rows = result.fetchall()
        data = [{"status": r.status, "count": r.cnt} for r in rows]
        total = sum(d["count"] for d in data)
        return StructuredResult(
            answer=f"{total} conflicts: " + ", ".join(f"{d['count']} {d['status']}" for d in data),
            data=data,
            query_type="conflict_summary",
        )

    # Latest documents
    if "latest" in query_lower or "recent" in query_lower:
        result = await session.execute(
            text(
                "SELECT id, title, status, quality_score, created_at "
                "FROM documents WHERE tenant_id = :tid AND status != 'deleted' "
                "ORDER BY created_at DESC LIMIT 10"
            ),
            {"tid": tenant_id},
        )
        rows = result.fetchall()
        data = [
            {
                "id": str(r.id),
                "title": r.title,
                "status": r.status,
                "quality_score": r.quality_score,
                "created_at": str(r.created_at),
            }
            for r in rows
        ]
        return StructuredResult(
            answer=f"Latest {len(data)} documents retrieved",
            data=data,
            query_type="latest_documents",
        )

    # Fallback: general document search by title
    result = await session.execute(
        text(
            "SELECT id, title, status, quality_score "
            "FROM documents WHERE tenant_id = :tid AND status != 'deleted' "
            "AND title ILIKE :pattern "
            "ORDER BY created_at DESC LIMIT 10"
        ),
        {"tid": tenant_id, "pattern": f"%{query}%"},
    )
    rows = result.fetchall()
    data = [
        {"id": str(r.id), "title": r.title, "status": r.status, "quality_score": r.quality_score}
        for r in rows
    ]
    return StructuredResult(
        answer=f"Found {len(data)} documents matching query",
        data=data,
        query_type="title_search",
    )
