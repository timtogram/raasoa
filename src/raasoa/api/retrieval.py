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

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant_async
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
    AnswerCitation,
    AnswerRequest,
    AnswerResponse,
    ChunkHit,
    ConfidenceInfo,
    FeedbackRequest,
    IndexHit,
    RetrieveRequest,
    RetrieveResponse,
    StructuredAnswer,
)
from raasoa.security.principal import (
    clamp_principal_override,
    expand_principal_ids,
    resolve_principal_async,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["retrieval"])


@router.post(
    "/retrieve",
    response_model=RetrieveResponse,
    operation_id="searchKnowledge",
    summary="Search the knowledge base",
    description=(
        "Search trusted enterprise knowledge and return ranked, "
        "source-attributed passages with a confidence score. Use this to "
        "answer any question that depends on the organization's documents, "
        "policies, or records. Supports metadata pre-filtering "
        "(e.g. only approved documents) and per-source filtering."
    ),
)
async def retrieve(
    http_request: Request,
    request: RetrieveRequest,
    session: AsyncSession = Depends(get_session),
) -> RetrieveResponse:
    """3-layer combined retrieval: Index → Structured → Hybrid Search."""
    principal = await resolve_principal_async(http_request)
    tenant_id = principal.tenant_id
    get_retrieve_limiter().check(str(tenant_id))

    # Quota check: monthly query limit
    from raasoa.middleware.metering import check_quota
    allowed, reason = await check_quota(session, tenant_id, "queries")
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    # Resolve the caller's principal closure automatically — a personal
    # API key scopes results without the caller needing to know/pass
    # anything. A legacy/tenant-wide key sees everything (principal_ids
    # stays None), exactly as before this feature existed. An explicit
    # request.principal_id can only narrow a personal key's own resolved
    # closure, never impersonate another principal.
    principal_ids = (
        None if principal.is_legacy_tenant_wide
        else await expand_principal_ids(session, tenant_id, principal.principal_id)  # type: ignore[arg-type]
    )
    effective_principal_id = clamp_principal_override(
        principal, request.principal_id, principal_ids,
    )
    # An explicit override narrows to exactly that one principal; no
    # override keeps the caller's full resolved closure (or None for a
    # legacy/tenant-wide key with no override — meaning "no filtering").
    final_principal_ids = (
        [effective_principal_id] if effective_principal_id is not None else principal_ids
    )

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
                principal_ids=final_principal_ids,
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
            principal_ids=final_principal_ids,
            source_type=request.source_type,
            doc_type=request.doc_type,
            metadata_filter=request.metadata_filter,
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
                document_title=r.document_title,
                source_url=r.source_url,
                source_type=r.source_type,
                source_name=r.source_name,
                page_number=r.page_number,
                source_location=r.source_location,
                doc_metadata=r.doc_metadata,
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

    # ── Usage metering ────────────────────────────────────
    from raasoa.middleware.metering import track_usage
    await track_usage(
        session, tenant_id, "retrieve", 1, {"routed_to": routed_to},
    )

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


@router.post(
    "/answer",
    response_model=AnswerResponse,
    operation_id="answerQuestion",
    summary="Answer a question with cited sources",
    description=(
        "Retrieve from the knowledge base and synthesize a direct answer "
        "grounded in the sources, with [n] citations. If the sources are "
        "too weak to answer confidently, RAASOA refuses instead of "
        "guessing (answered=false) — no hallucinations."
    ),
)
async def answer(
    http_request: Request,
    request: AnswerRequest,
    session: AsyncSession = Depends(get_session),
) -> AnswerResponse:
    """Grounded answer with citations, or an honest refusal."""
    principal = await resolve_principal_async(http_request)
    tenant_id = principal.tenant_id
    get_retrieve_limiter().check(str(tenant_id))

    from raasoa.retrieval.answer import (
        REFUSAL_TEXT,
        SourceChunk,
        is_insufficient,
        synthesize_answer,
        valid_citation_numbers,
    )

    principal_ids = (
        None if principal.is_legacy_tenant_wide
        else await expand_principal_ids(session, tenant_id, principal.principal_id)  # type: ignore[arg-type]
    )
    effective_principal_id = clamp_principal_override(
        principal, request.principal_id, principal_ids,
    )
    final_principal_ids = (
        [effective_principal_id] if effective_principal_id is not None else principal_ids
    )

    provider = get_embedding_provider()
    reranker = get_reranker()
    search_results = await search(
        session=session,
        query=request.query,
        tenant_id=tenant_id,
        embedding_provider=provider,
        top_k=request.top_k * 3,
        principal_ids=final_principal_ids,
        source_type=request.source_type,
        metadata_filter=request.metadata_filter,
    )
    search_results = await reranker.rerank(
        request.query, search_results, request.top_k,
    )
    conf = compute_confidence(search_results)
    conf_info = ConfidenceInfo(
        retrieval_confidence=conf.retrieval_confidence,
        source_count=conf.source_count,
        top_score=conf.top_score,
        answerable=conf.answerable,
    )

    # Track usage regardless of outcome.
    from raasoa.middleware.metering import track_usage
    await track_usage(session, tenant_id, "answer", 1, {})

    # Honest refusal: too little to go on.
    if not search_results or conf.retrieval_confidence < request.min_confidence:
        return AnswerResponse(
            query=request.query,
            answered=False,
            answer=REFUSAL_TEXT,
            citations=[],
            confidence=conf_info,
            refusal_reason=(
                f"retrieval_confidence {conf.retrieval_confidence:.2f} "
                f"< min_confidence {request.min_confidence:.2f}"
                if search_results else "no matching sources"
            ),
        )

    chunks = [
        SourceChunk(
            n=i + 1,
            chunk_id=str(r.chunk_id),
            document_id=str(r.document_id),
            document_title=r.document_title,
            source_url=r.source_url,
            source_location=r.source_location,
            text=r.chunk_text,
        )
        for i, r in enumerate(search_results)
    ]

    raw = await synthesize_answer(request.query, chunks)

    if is_insufficient(raw):
        return AnswerResponse(
            query=request.query,
            answered=False,
            answer=REFUSAL_TEXT,
            citations=[],
            confidence=conf_info,
            refusal_reason="model judged the sources insufficient",
        )

    # Citation-required guard: a grounded answer MUST cite its sources.
    # An answer with zero [n] markers is either leaked chain-of-thought
    # (weak models reason in prose) or an ungrounded claim — refuse either
    # way. This enforces the product promise ("every fact cited") and is
    # model-agnostic, so it holds even when a small model ignores the
    # "say INSUFFICIENT" instruction.
    valid_used = valid_citation_numbers(raw, len(chunks))
    if not valid_used:
        return AnswerResponse(
            query=request.query,
            answered=False,
            answer=REFUSAL_TEXT,
            citations=[],
            confidence=conf_info,
            refusal_reason="answer was not grounded in any cited source",
        )

    citations = [
        AnswerCitation(
            n=c.n,
            document_id=c.document_id,
            document_title=c.document_title,
            source_url=c.source_url,
            source_location=c.source_location,
            chunk_id=c.chunk_id,
            quote=c.text[:280],
        )
        for c in chunks
        if c.n in valid_used
    ]

    return AnswerResponse(
        query=request.query,
        answered=True,
        answer=raw,
        citations=citations,
        confidence=conf_info,
        refusal_reason=None,
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
    tenant_id = await resolve_tenant_async(http_request)

    await store_feedback(
        session,
        FeedbackSignal(
            query=feedback.query,
            chunk_id=uuid.UUID(feedback.chunk_id),
            document_id=uuid.UUID(feedback.document_id),
            rating=feedback.rating,
            tenant_id=tenant_id,
            outcome=feedback.outcome,
            outcome_context=feedback.outcome_context,
        ),
    )
    return {"status": "recorded"}
