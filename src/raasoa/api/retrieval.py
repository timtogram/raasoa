import logging
import time

from fastapi import APIRouter, Depends
from fastapi import Request as FastAPIRequest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.rate_limit import get_retrieve_limiter
from raasoa.providers.factory import get_embedding_provider
from raasoa.retrieval.confidence import compute_confidence
from raasoa.retrieval.factory import get_reranker
from raasoa.retrieval.hybrid_search import search
from raasoa.retrieval.query_router import QueryType, route_query
from raasoa.retrieval.structured import structured_query
from raasoa.schemas.retrieval import (
    ChunkHit,
    ConfidenceInfo,
    RetrieveRequest,
    RetrieveResponse,
    StructuredAnswer,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["retrieval"])


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    http_request: FastAPIRequest,
    request: RetrieveRequest,
    session: AsyncSession = Depends(get_session),
) -> RetrieveResponse:
    """Hybrid search with query routing, RRF fusion, and confidence scoring."""
    get_retrieve_limiter().check(str(request.tenant_id))

    start_time = time.monotonic()

    # 1. Route query
    routing = route_query(request.query)

    structured: StructuredAnswer | None = None
    results_list: list[ChunkHit] = []
    confidence_info: ConfidenceInfo | None = None

    # 2. Handle structured queries
    if routing.query_type == QueryType.STRUCTURED:
        try:
            sq_result = await structured_query(session, request.query, request.tenant_id)
            structured = StructuredAnswer(
                answer=sq_result.answer,
                data=sq_result.data,
                query_type=sq_result.query_type,
            )
        except Exception:
            logger.warning("Structured query failed, falling back to RAG")
            routing = routing.__class__(
                query_type=QueryType.RAG, confidence=0.5, reason="structured_fallback"
            )

    # 3. Handle RAG queries (or fallback)
    if routing.query_type == QueryType.RAG:
        provider = get_embedding_provider()
        reranker = get_reranker()

        search_results = await search(
            session=session,
            query=request.query,
            tenant_id=request.tenant_id,
            embedding_provider=provider,
            top_k=request.top_k * 3,
        )

        search_results = await reranker.rerank(request.query, search_results, request.top_k)
        confidence = compute_confidence(search_results)

        results_list = [
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
            for r in search_results
        ]
        confidence_info = ConfidenceInfo(
            retrieval_confidence=confidence.retrieval_confidence,
            source_count=confidence.source_count,
            top_score=confidence.top_score,
            answerable=confidence.answerable,
        )

    # 4. Write retrieval audit log
    latency_ms = int((time.monotonic() - start_time) * 1000)
    try:
        chunk_ids = [r.chunk_id for r in results_list] if results_list else None
        await session.execute(
            text(
                "INSERT INTO retrieval_logs "
                "(tenant_id, query_text, routed_to, chunks_returned, "
                " retrieval_confidence, answerable, latency_ms) "
                "VALUES (:tid, :query, :routed, :chunks, :conf, :ans, :lat)"
            ),
            {
                "tid": request.tenant_id,
                "query": request.query,
                "routed": routing.query_type.value,
                "chunks": chunk_ids,
                "conf": confidence_info.retrieval_confidence if confidence_info else None,
                "ans": confidence_info.answerable if confidence_info else None,
                "lat": latency_ms,
            },
        )
        await session.commit()
    except Exception:
        logger.debug("Failed to write retrieval log", exc_info=True)
        await session.rollback()

    return RetrieveResponse(
        query=request.query,
        routed_to=routing.query_type.value,
        routing_reason=routing.reason,
        results=results_list,
        structured=structured,
        confidence=confidence_info
        or ConfidenceInfo(
            retrieval_confidence=0.0, source_count=0, top_score=0.0, answerable=False
        ),
    )
