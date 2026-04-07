import logging
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant
from raasoa.middleware.rate_limit import get_retrieve_limiter
from raasoa.providers.factory import get_embedding_provider
from raasoa.retrieval.confidence import compute_confidence
from raasoa.retrieval.factory import get_reranker
from raasoa.retrieval.feedback import FeedbackSignal, store_feedback
from raasoa.retrieval.hybrid_search import search
from raasoa.retrieval.query_router import QueryType, route_query
from raasoa.retrieval.structured import structured_query
from raasoa.schemas.retrieval import (
    ChunkHit,
    ConfidenceInfo,
    FeedbackRequest,
    RetrieveRequest,
    RetrieveResponse,
    StructuredAnswer,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["retrieval"])


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    http_request: Request,
    request: RetrieveRequest,
    session: AsyncSession = Depends(get_session),
) -> RetrieveResponse:
    """Hybrid search with auth, ACL, query routing, and confidence."""
    tenant_id = resolve_tenant(http_request)
    get_retrieve_limiter().check(str(tenant_id))

    start_time = time.monotonic()
    routing = route_query(request.query)

    structured: StructuredAnswer | None = None
    results_list: list[ChunkHit] = []
    confidence_info: ConfidenceInfo | None = None

    if routing.query_type == QueryType.STRUCTURED:
        try:
            sq_result = await structured_query(
                session, request.query, tenant_id,
            )
            structured = StructuredAnswer(
                answer=sq_result.answer,
                data=sq_result.data,
                query_type=sq_result.query_type,
            )
        except Exception:
            logger.warning("Structured query failed, falling back to RAG")
            routing = routing.__class__(
                query_type=QueryType.RAG,
                confidence=0.5,
                reason="structured_fallback",
            )

    if routing.query_type == QueryType.RAG:
        provider = get_embedding_provider()
        reranker = get_reranker()

        search_results = await search(
            session=session,
            query=request.query,
            tenant_id=tenant_id,
            embedding_provider=provider,
            top_k=request.top_k * 3,
            principal_id=request.principal_id,
        )

        search_results = await reranker.rerank(
            request.query, search_results, request.top_k,
        )
        confidence = compute_confidence(search_results)

        results_list = [
            ChunkHit(
                chunk_id=str(r.chunk_id),
                document_id=str(r.document_id),
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

    latency_ms = int((time.monotonic() - start_time) * 1000)
    try:
        chunk_ids = [r.chunk_id for r in results_list] if results_list else None
        await session.execute(
            text(
                "INSERT INTO retrieval_logs "
                "(tenant_id, query_text, routed_to, chunks_returned, "
                " retrieval_confidence, answerable, latency_ms) "
                "VALUES (:tid, :query, :routed, :chunks, "
                " :conf, :ans, :lat)"
            ),
            {
                "tid": tenant_id,
                "query": request.query,
                "routed": routing.query_type.value,
                "chunks": chunk_ids,
                "conf": confidence_info.retrieval_confidence
                if confidence_info
                else None,
                "ans": confidence_info.answerable
                if confidence_info
                else None,
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
            retrieval_confidence=0.0,
            source_count=0,
            top_score=0.0,
            answerable=False,
        ),
    )


@router.post("/retrieve/feedback")
async def submit_feedback(
    http_request: Request,
    feedback: FeedbackRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Submit feedback on a retrieval result.

    Positive feedback boosts the chunk's ranking for similar future queries.
    Negative feedback demotes it. Over time, this makes retrieval smarter.
    """
    tenant_id = resolve_tenant(http_request)
    import uuid

    await store_feedback(
        session,
        FeedbackSignal(
            query=feedback.query,
            chunk_id=uuid.UUID(feedback.chunk_id),
            document_id=uuid.UUID(feedback.document_id),
            rating=feedback.rating,
            tenant_id=tenant_id,
        ),
    )
    return {"status": "recorded"}
