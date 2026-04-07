"""Retrieval API — 3-layer combined search.

Layer 1: Knowledge Index (< 5ms, 100% confidence for factual queries)
Layer 2: Structured SQL (< 20ms, for aggregation/metadata queries)
Layer 3: Hybrid Search (200-800ms, for semantic/conceptual queries)

All three layers are tried in order. Results are combined in one response
so the consuming agent can pick the best answer.
"""

import logging
import time
import uuid

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
from raasoa.retrieval.knowledge_index import lookup as index_lookup
from raasoa.retrieval.query_router import QueryType, route_query
from raasoa.retrieval.structured import structured_query
from raasoa.schemas.retrieval import (
    ChunkHit,
    ConfidenceInfo,
    FeedbackRequest,
    IndexHit,
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
    """3-layer combined retrieval: Index → Structured → Hybrid Search."""
    tenant_id = resolve_tenant(http_request)
    get_retrieve_limiter().check(str(tenant_id))

    start_time = time.monotonic()

    index_hits: list[IndexHit] = []
    structured: StructuredAnswer | None = None
    results_list: list[ChunkHit] = []
    confidence_info: ConfidenceInfo | None = None
    routed_to = "rag"
    routing_reason = "default_rag"

    # ── Layer 1: Knowledge Index Lookup ──────────────────
    try:
        idx_result = await index_lookup(session, tenant_id, request.query)
        if idx_result.found:
            index_hits = [
                IndexHit(
                    subject=e.subject,
                    predicate=e.predicate,
                    value=e.value,
                    confidence=e.confidence,
                    source_documents=e.source_documents,
                )
                for e in idx_result.entries
            ]
            routed_to = "index"
            routing_reason = "knowledge_index_hit"
    except Exception:
        logger.debug("Index lookup failed", exc_info=True)

    # ── Layer 2: Query Routing (Structured vs RAG) ───────
    routing = route_query(request.query)

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
            if not index_hits:
                routed_to = "structured"
                routing_reason = routing.reason
        except Exception:
            logger.warning("Structured query failed, falling back to RAG")
            routing = routing.__class__(
                query_type=QueryType.RAG,
                confidence=0.5,
                reason="structured_fallback",
            )

    # ── Layer 3: Hybrid Search ───────────────────────────
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
        if not index_hits and not structured:
            routed_to = "rag"
            routing_reason = routing.reason

    # ── Confidence: boost if index hit ───────────────────
    if index_hits and not confidence_info:
        confidence_info = ConfidenceInfo(
            retrieval_confidence=max(h.confidence for h in index_hits),
            source_count=len(
                {d for h in index_hits for d in h.source_documents}
            ),
            top_score=index_hits[0].confidence,
            answerable=True,
        )

    # ── Audit log ────────────────────────────────────────
    latency_ms = int((time.monotonic() - start_time) * 1000)
    try:
        chunk_ids = (
            [r.chunk_id for r in results_list] if results_list else None
        )
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
                "routed": routed_to,
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
        routed_to=routed_to,
        routing_reason=routing_reason,
        index_hits=index_hits,
        structured=structured,
        results=results_list,
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

    Positive feedback boosts the chunk for similar future queries.
    """
    tenant_id = resolve_tenant(http_request)

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
