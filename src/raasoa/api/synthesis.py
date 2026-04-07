"""Knowledge Synthesis API — compiled knowledge for agents.

Agents get better answers from synthesized topic summaries than from
raw chunks. This endpoint exposes the compilation layer.

GET  /v1/synthesis          — list all synthesized topics
GET  /v1/synthesis/{topic}  — get synthesis for a specific topic
POST /v1/synthesis/compile  — trigger compilation for a topic (or all)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant

router = APIRouter(prefix="/v1/synthesis", tags=["synthesis"])


class SynthesisResponse(BaseModel):
    topic: str
    summary: str
    claim_count: int
    source_documents: int
    confidence: float | None
    status: str
    updated_at: str | None


class CompileRequest(BaseModel):
    topic: str | None = Field(
        default=None,
        description="Compile a specific topic. Omit to compile all topics.",
    )


@router.get("", response_model=list[SynthesisResponse])
async def list_syntheses(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[SynthesisResponse]:
    """List all synthesized knowledge topics."""
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT topic, summary, claim_count, "
            "  jsonb_array_length(COALESCE(source_document_ids, '[]'::jsonb)) "
            "    as source_documents, "
            "  confidence, status, updated_at "
            "FROM knowledge_syntheses "
            "WHERE tenant_id = :tid AND status = 'active' "
            "ORDER BY claim_count DESC "
            "LIMIT :lim"
        ),
        {"tid": tenant_id, "lim": limit},
    )
    return [
        SynthesisResponse(
            topic=r.topic,
            summary=r.summary,
            claim_count=r.claim_count,
            source_documents=r.source_documents,
            confidence=r.confidence,
            status=r.status,
            updated_at=str(r.updated_at) if r.updated_at else None,
        )
        for r in result.fetchall()
    ]


@router.get("/{topic}", response_model=SynthesisResponse)
async def get_synthesis(
    request: Request,
    topic: str,
    session: AsyncSession = Depends(get_session),
) -> SynthesisResponse:
    """Get the synthesized knowledge for a specific topic."""
    tenant_id = resolve_tenant(request)

    result = await session.execute(
        text(
            "SELECT topic, summary, claim_count, "
            "  jsonb_array_length(COALESCE(source_document_ids, '[]'::jsonb)) "
            "    as source_documents, "
            "  confidence, status, updated_at "
            "FROM knowledge_syntheses "
            "WHERE tenant_id = :tid AND topic = :topic"
        ),
        {"tid": tenant_id, "topic": topic},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"No synthesis for topic: {topic}")

    return SynthesisResponse(
        topic=row.topic,
        summary=row.summary,
        claim_count=row.claim_count,
        source_documents=row.source_documents,
        confidence=row.confidence,
        status=row.status,
        updated_at=str(row.updated_at) if row.updated_at else None,
    )


@router.post("/build-index")
async def build_knowledge_index(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Rebuild the knowledge index from active claims.

    The index enables sub-5ms factual lookups without embedding.
    Run this after ingesting new documents or resolving conflicts.
    """
    tenant_id = resolve_tenant(request)

    from raasoa.retrieval.knowledge_index import build_index

    stats = await build_index(session, tenant_id)
    return {"status": "built", **stats}


@router.post("/compile")
async def compile_synthesis(
    request: Request,
    body: CompileRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Trigger knowledge compilation.

    Compiles claims into synthesized summaries. If topic is specified,
    compiles only that topic. Otherwise compiles all topics with claims.
    """
    tenant_id = resolve_tenant(request)

    from raasoa.quality.synthesis import synthesize_all_topics, synthesize_topic

    if body.topic:
        result = await synthesize_topic(session, tenant_id, body.topic)
        return {"compiled": [result]}
    else:
        results = await synthesize_all_topics(session, tenant_id)
        return {
            "compiled": results,
            "total_topics": len(results),
        }
