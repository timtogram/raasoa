"""Hybrid Search — Dense + BM25 + Reciprocal Rank Fusion.

Supports pre-filtering by source_type and doc_type BEFORE vector scan.
This is the MemPalace insight: structural metadata filtering before
semantic search improves accuracy by up to 34%.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.providers.base import EmbeddingProvider


@dataclass
class SearchResult:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    chunk_text: str
    section_title: str | None
    chunk_type: str
    score: float
    semantic_rank: int | None = None
    lexical_rank: int | None = None


async def hybrid_search(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    tenant_id: uuid.UUID,
    top_k: int = 20,
    semantic_weight: float = 1.0,
    lexical_weight: float = 1.0,
    rrf_k: int = 60,
    principal_id: str | None = None,
    source_type: str | None = None,
    doc_type: str | None = None,
) -> list[SearchResult]:
    """Hybrid search with optional source/type pre-filtering.

    Pre-filtering narrows the search space BEFORE vector scan,
    significantly improving precision for targeted queries.

    Args:
        source_type: Filter to a specific source (e.g. "sharepoint", "jira")
        doc_type: Filter to a document type (e.g. "pdf", "policy")
        principal_id: ACL filter — only docs accessible to this user
    """
    # Build dynamic filter clauses
    extra_filters = ""
    params: dict[str, Any] = {
        "query_embedding": str(query_embedding),
        "query": query,
        "tenant_id": tenant_id,
        "candidate_limit": top_k * 3,
        "semantic_weight": semantic_weight,
        "lexical_weight": lexical_weight,
        "rrf_k": rrf_k,
        "top_k": top_k,
    }

    if source_type:
        extra_filters += (
            " AND d.source_id IN ("
            "   SELECT s.id FROM sources s"
            "   WHERE s.source_type = :source_type"
            " )"
        )
        params["source_type"] = source_type

    if doc_type:
        extra_filters += " AND d.doc_type = :doc_type"
        params["doc_type"] = doc_type

    if principal_id:
        extra_filters += (
            " AND (NOT EXISTS ("
            "   SELECT 1 FROM acl_entries a2 WHERE a2.document_id = d.id"
            " ) OR EXISTS ("
            "   SELECT 1 FROM acl_entries a WHERE a.document_id = d.id"
            "   AND a.principal_id = :principal_id"
            "   AND a.permission IN ('read', 'write', 'admin')"
            " ))"
        )
        params["principal_id"] = principal_id

    base_where = (
        "d.tenant_id = :tenant_id"
        " AND d.status = 'indexed'"
        " AND d.review_status NOT IN"
        " ('quarantined', 'rejected', 'superseded')"
        f"{extra_filters}"
    )

    sql = text(f"""
        WITH semantic AS (
            SELECT
                c.id, c.document_id, c.chunk_text,
                c.section_title, c.chunk_type,
                ROW_NUMBER() OVER (
                    ORDER BY c.embedding <=> :query_embedding
                ) AS rn
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            WHERE {base_where}
              AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> :query_embedding
            LIMIT :candidate_limit
        ),
        lexical AS (
            SELECT
                c.id, c.document_id, c.chunk_text,
                c.section_title, c.chunk_type,
                ROW_NUMBER() OVER (
                    ORDER BY ts_rank(
                        c.tsv, plainto_tsquery('simple', :query)
                    ) DESC
                ) AS rn
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            WHERE {base_where}
              AND c.tsv @@ plainto_tsquery('simple', :query)
            ORDER BY ts_rank(
                c.tsv, plainto_tsquery('simple', :query)
            ) DESC
            LIMIT :candidate_limit
        )
        ,feedback AS (
            SELECT chunk_id,
                COALESCE(SUM(rating), 0) /
                    GREATEST(COUNT(*), 1) * 0.1 AS boost
            FROM retrieval_feedback
            WHERE tenant_id = :tenant_id
            GROUP BY chunk_id
        )
        SELECT
            COALESCE(s.id, l.id) AS chunk_id,
            COALESCE(s.document_id, l.document_id) AS document_id,
            COALESCE(s.chunk_text, l.chunk_text) AS chunk_text,
            COALESCE(s.section_title, l.section_title) AS section_title,
            COALESCE(s.chunk_type, l.chunk_type) AS chunk_type,
            (
                COALESCE(:semantic_weight * (1.0 / (:rrf_k + s.rn)), 0) +
                COALESCE(:lexical_weight * (1.0 / (:rrf_k + l.rn)), 0) +
                COALESCE(fb.boost, 0)
            ) AS rrf_score,
            s.rn AS semantic_rank,
            l.rn AS lexical_rank
        FROM semantic s
        FULL OUTER JOIN lexical l ON s.id = l.id
        LEFT JOIN feedback fb
            ON fb.chunk_id = COALESCE(s.id, l.id)
        ORDER BY rrf_score DESC
        LIMIT :top_k
    """)

    result = await session.execute(sql, params)
    return [
        SearchResult(
            chunk_id=row.chunk_id,
            document_id=row.document_id,
            chunk_text=row.chunk_text,
            section_title=row.section_title,
            chunk_type=row.chunk_type,
            score=float(row.rrf_score),
            semantic_rank=row.semantic_rank,
            lexical_rank=row.lexical_rank,
        )
        for row in result.fetchall()
    ]


async def search(
    session: AsyncSession,
    query: str,
    tenant_id: uuid.UUID,
    embedding_provider: EmbeddingProvider,
    top_k: int = 10,
    principal_id: str | None = None,
    source_type: str | None = None,
    doc_type: str | None = None,
) -> list[SearchResult]:
    """High-level search: embed query, then hybrid search."""
    embeddings = await embedding_provider.embed([query])
    query_embedding = embeddings[0]

    return await hybrid_search(
        session=session,
        query=query,
        query_embedding=query_embedding,
        tenant_id=tenant_id,
        top_k=top_k,
        principal_id=principal_id,
        source_type=source_type,
        doc_type=doc_type,
    )
