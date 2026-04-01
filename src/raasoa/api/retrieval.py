from fastapi import APIRouter, Depends
from fastapi import Request as FastAPIRequest
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.rate_limit import get_retrieve_limiter
from raasoa.providers.factory import get_embedding_provider
from raasoa.retrieval.confidence import compute_confidence
from raasoa.retrieval.hybrid_search import search
from raasoa.retrieval.reranker import PassthroughReranker
from raasoa.schemas.retrieval import (
    ChunkHit,
    ConfidenceInfo,
    RetrieveRequest,
    RetrieveResponse,
)

router = APIRouter(prefix="/v1", tags=["retrieval"])


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    http_request: FastAPIRequest,
    request: RetrieveRequest,
    session: AsyncSession = Depends(get_session),
) -> RetrieveResponse:
    """Hybrid search with RRF fusion and confidence scoring."""
    get_retrieve_limiter().check(str(request.tenant_id))
    provider = get_embedding_provider()
    reranker = PassthroughReranker()

    # 1. Hybrid search (dense + BM25 + RRF)
    results = await search(
        session=session,
        query=request.query,
        tenant_id=request.tenant_id,
        embedding_provider=provider,
        top_k=request.top_k * 3,  # Over-fetch for reranking
    )

    # 2. Rerank
    results = await reranker.rerank(request.query, results, request.top_k)

    # 3. Confidence
    confidence = compute_confidence(results)

    return RetrieveResponse(
        query=request.query,
        results=[
            ChunkHit(
                chunk_id=r.chunk_id,
                document_id=r.document_id,
                text=r.chunk_text,
                section_title=r.section_title,
                chunk_type=r.chunk_type,
                score=r.score,
                semantic_rank=r.semantic_rank,
                lexical_rank=r.lexical_rank,
            )
            for r in results
        ],
        confidence=ConfidenceInfo(
            retrieval_confidence=confidence.retrieval_confidence,
            source_count=confidence.source_count,
            top_score=confidence.top_score,
            answerable=confidence.answerable,
        ),
    )
