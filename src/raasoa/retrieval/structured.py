"""Structured queries: Answer metadata/aggregation questions via direct SQL.

These bypass the vector/BM25 search and query document metadata directly.
"""
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.security.principal import acl_predicate_sql


@dataclass
class StructuredResult:
    answer: str
    data: list[dict[str, Any]]
    query_type: str


def _acl_filter(principal_ids: list[str] | None) -> str:
    """The document-touching branches below all alias documents as `d` and
    join sources as `s` — see the module-level queries for exactly where.
    Returns "" (no filter, today's behavior) when principal_ids is None.
    Uses tenant_id_param="tid" to match this module's existing :tid
    parameter naming convention (acl_predicate_sql defaults to
    "tenant_id", which isn't a key structured_query's params dict uses).
    """
    if principal_ids is None:
        return ""
    return acl_predicate_sql(doc_alias="d", source_alias="s", tenant_id_param="tid")


async def structured_query(
    session: AsyncSession,
    query: str,
    tenant_id: uuid.UUID,
    principal_ids: list[str] | None = None,
) -> StructuredResult:
    """Execute a structured query against document metadata.

    Supports:
    - Document counts and listings
    - Quality score aggregations
    - Conflict/review status summaries

    principal_ids: the caller's resolved principal closure (see
    raasoa.security.principal). None means no ACL filtering (today's
    behavior for unauthenticated/legacy callers); every document-touching
    branch below applies the same ACL predicate as hybrid_search so a
    restricted source can't be enumerated through aggregation/listing
    queries instead of semantic search.
    """
    query_lower = query.lower()
    acl_filter = _acl_filter(principal_ids)
    params: dict[str, Any] = {"tid": tenant_id}
    if principal_ids is not None:
        params["principal_ids"] = principal_ids

    # Document count
    if "how many" in query_lower and "document" in query_lower:
        result = await session.execute(
            text(
                "SELECT COUNT(*) as total, "
                "COUNT(*) FILTER (WHERE d.status = 'indexed') as indexed, "
                "COUNT(*) FILTER (WHERE d.status = 'quarantined') as quarantined "
                "FROM documents d "
                "JOIN sources s ON s.id = d.source_id "
                "WHERE d.tenant_id = :tid AND d.status != 'deleted'"
                f"{acl_filter}"
            ),
            params,
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
                "ROUND(AVG(d.quality_score)::numeric, 2) as avg_score, "
                "ROUND(MIN(d.quality_score)::numeric, 2) as min_score, "
                "ROUND(MAX(d.quality_score)::numeric, 2) as max_score, "
                "COUNT(*) FILTER (WHERE d.quality_score >= 0.8) as high_quality, "
                "COUNT(*) FILTER (WHERE d.quality_score < 0.5) as low_quality "
                "FROM documents d "
                "JOIN sources s ON s.id = d.source_id "
                "WHERE d.tenant_id = :tid "
                "AND d.status != 'deleted' AND d.quality_score IS NOT NULL"
                f"{acl_filter}"
            ),
            params,
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

    # Conflict summary — restricted to conflicts where at least one side
    # is a document the caller can see (an ACL-visible mention that a
    # conflict exists is fine even if the other side is restricted; the
    # actual restricted document's title/content is never included here).
    if "conflict" in query_lower:
        conflict_acl = ""
        if principal_ids is not None:
            visible_doc = acl_predicate_sql(
                doc_alias="vd", source_alias="vs", tenant_id_param="tid",
            ).removeprefix(" AND ")
            conflict_acl = (
                " AND (EXISTS ("
                "   SELECT 1 FROM documents vd JOIN sources vs ON vs.id = vd.source_id"
                "   WHERE vd.id = cc.document_a_id AND "
                f"  {visible_doc}"
                " ) OR EXISTS ("
                "   SELECT 1 FROM documents vd JOIN sources vs ON vs.id = vd.source_id"
                "   WHERE vd.id = cc.document_b_id AND "
                f"  {visible_doc}"
                " ))"
            )
        result = await session.execute(
            text(
                "SELECT status, COUNT(*) as cnt "
                "FROM conflict_candidates cc "
                "WHERE cc.tenant_id = :tid"
                f"{conflict_acl} "
                "GROUP BY status ORDER BY cnt DESC"
            ),
            params,
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
                "SELECT d.id, d.title, d.status, d.quality_score, d.created_at "
                "FROM documents d "
                "JOIN sources s ON s.id = d.source_id "
                "WHERE d.tenant_id = :tid AND d.status != 'deleted'"
                f"{acl_filter} "
                "ORDER BY d.created_at DESC LIMIT 10"
            ),
            params,
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
    params["pattern"] = f"%{query}%"
    result = await session.execute(
        text(
            "SELECT d.id, d.title, d.status, d.quality_score "
            "FROM documents d "
            "JOIN sources s ON s.id = d.source_id "
            "WHERE d.tenant_id = :tid AND d.status != 'deleted' "
            "AND d.title ILIKE :pattern"
            f"{acl_filter} "
            "ORDER BY d.created_at DESC LIMIT 10"
        ),
        params,
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
