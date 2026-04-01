import uuid
from dataclasses import dataclass

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
) -> list[SearchResult]:
    """Hybrid search combining dense vector + BM25 with Reciprocal Rank Fusion."""

    sql = text("""
        WITH semantic AS (
            SELECT
                c.id,
                c.document_id,
                c.chunk_text,
                c.section_title,
                c.chunk_type,
                ROW_NUMBER() OVER (ORDER BY c.embedding <=> :query_embedding) AS rn
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            WHERE d.tenant_id = :tenant_id
              AND d.status = 'indexed'
              AND d.review_status NOT IN ('quarantined', 'rejected', 'superseded')
              AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> :query_embedding
            LIMIT :candidate_limit
        ),
        lexical AS (
            SELECT
                c.id,
                c.document_id,
                c.chunk_text,
                c.section_title,
                c.chunk_type,
                ROW_NUMBER() OVER (
                    ORDER BY ts_rank(c.tsv, plainto_tsquery('simple', :query)) DESC
                ) AS rn
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            WHERE d.tenant_id = :tenant_id
              AND d.status = 'indexed'
              AND d.review_status NOT IN ('quarantined', 'rejected', 'superseded')
              AND c.tsv @@ plainto_tsquery('simple', :query)
            ORDER BY ts_rank(c.tsv, plainto_tsquery('simple', :query)) DESC
            LIMIT :candidate_limit
        )
        SELECT
            COALESCE(s.id, l.id) AS chunk_id,
            COALESCE(s.document_id, l.document_id) AS document_id,
            COALESCE(s.chunk_text, l.chunk_text) AS chunk_text,
            COALESCE(s.section_title, l.section_title) AS section_title,
            COALESCE(s.chunk_type, l.chunk_type) AS chunk_type,
            (
                COALESCE(:semantic_weight * (1.0 / (:rrf_k + s.rn)), 0) +
                COALESCE(:lexical_weight * (1.0 / (:rrf_k + l.rn)), 0)
            ) AS rrf_score,
            s.rn AS semantic_rank,
            l.rn AS lexical_rank
        FROM semantic s
        FULL OUTER JOIN lexical l ON s.id = l.id
        ORDER BY rrf_score DESC
        LIMIT :top_k
    """)

    result = await session.execute(
        sql,
        {
            "query_embedding": str(query_embedding),
            "query": query,
            "tenant_id": tenant_id,
            "candidate_limit": top_k * 3,
            "semantic_weight": semantic_weight,
            "lexical_weight": lexical_weight,
            "rrf_k": rrf_k,
            "top_k": top_k,
        },
    )

    rows = result.fetchall()
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
        for row in rows
    ]


async def search(
    session: AsyncSession,
    query: str,
    tenant_id: uuid.UUID,
    embedding_provider: EmbeddingProvider,
    top_k: int = 10,
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
    )
