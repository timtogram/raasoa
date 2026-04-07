"""Knowledge Synthesis — LLM-compiled topic summaries.

Inspired by Karpathy's "compilation" approach: instead of just storing
raw chunks, the LLM reads related claims and produces a synthesized
summary per topic. This creates a "compiled knowledge base" that
accumulates understanding over time.

Each synthesis:
- Groups related claims by subject
- Asks the LLM to write a concise, factual summary
- Tracks which documents and claims contributed
- Gets updated when new claims arrive for the same topic
"""

from __future__ import annotations

import json
import logging
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings

logger = logging.getLogger(__name__)

SYNTHESIS_PROMPT = """/no_think
You are a knowledge compiler. Given a set of factual claims about a topic,
write a concise, accurate summary that synthesizes all claims into a
coherent knowledge article.

Rules:
- Only state facts supported by the claims
- If claims contradict each other, note the contradiction explicitly
- Use clear, direct language
- Include specific numbers, dates, and names from the claims
- Keep it under 500 words
- Write in the same language as the majority of claims

Topic: {topic}

Claims:
{claims_text}

Summary:"""


async def synthesize_topic(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    topic: str,
) -> dict[str, str | int | float | None]:
    """Synthesize all active claims for a topic into a summary.

    Groups claims by subject, calls LLM to compile, stores result.
    Returns the synthesis record.
    """
    # Fetch all active claims for this topic (subject)
    result = await session.execute(
        text(
            "SELECT c.id, c.subject, c.predicate, c.object_value, "
            "c.confidence, c.document_id "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE d.tenant_id = :tid "
            "AND c.subject = :topic "
            "AND c.status = 'active' "
            "ORDER BY c.confidence DESC"
        ),
        {"tid": tenant_id, "topic": topic},
    )
    claims = result.fetchall()

    if not claims:
        return {"status": "no_claims", "topic": topic}

    # Format claims for LLM
    claims_text = "\n".join(
        f"- {c.predicate}: {c.object_value} (confidence: {c.confidence:.0%})"
        for c in claims
    )

    doc_ids = list({str(c.document_id) for c in claims})
    claim_ids = [str(c.id) for c in claims]

    # Call LLM
    try:
        prompt = SYNTHESIS_PROMPT.format(
            topic=topic, claims_text=claims_text,
        )
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": settings.ollama_chat_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 2048},
                },
            )
            resp.raise_for_status()
            import re
            summary = resp.json().get("response", "").strip()
            summary = re.sub(
                r"<think>.*?</think>", "", summary, flags=re.DOTALL,
            ).strip()

    except Exception:
        logger.warning("Synthesis LLM call failed for topic '%s'", topic)
        # Fallback: mechanical summary
        summary = f"Topic: {topic}\n\n"
        for c in claims:
            summary += f"- {c.predicate}: {c.object_value}\n"

    # Upsert synthesis
    existing = await session.execute(
        text(
            "SELECT id FROM knowledge_syntheses "
            "WHERE tenant_id = :tid AND topic = :topic"
        ),
        {"tid": tenant_id, "topic": topic},
    )
    row = existing.first()

    if row:
        await session.execute(
            text(
                "UPDATE knowledge_syntheses SET "
                "summary = :summary, "
                "source_document_ids = CAST(:doc_ids AS jsonb), "
                "source_claim_ids = CAST(:claim_ids AS jsonb), "
                "claim_count = :count, "
                "updated_at = now() "
                "WHERE id = :sid"
            ),
            {
                "sid": row.id,
                "summary": summary,
                "doc_ids": json.dumps(doc_ids),
                "claim_ids": json.dumps(claim_ids),
                "count": len(claims),
            },
        )
        synthesis_id = row.id
    else:
        synthesis_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO knowledge_syntheses "
                "(id, tenant_id, topic, summary, source_document_ids, "
                " source_claim_ids, claim_count, confidence, status) "
                "VALUES (:id, :tid, :topic, :summary, "
                " CAST(:doc_ids AS jsonb), CAST(:claim_ids AS jsonb), "
                " :count, :conf, 'active')"
            ),
            {
                "id": synthesis_id,
                "tid": tenant_id,
                "topic": topic,
                "summary": summary,
                "doc_ids": json.dumps(doc_ids),
                "claim_ids": json.dumps(claim_ids),
                "count": len(claims),
                "conf": sum(c.confidence for c in claims) / len(claims),
            },
        )

    await session.commit()

    logger.info(
        "Synthesized topic '%s': %d claims → %d words",
        topic, len(claims), len(summary.split()),
    )

    return {
        "status": "synthesized",
        "topic": topic,
        "claim_count": len(claims),
        "summary_length": len(summary),
        "source_documents": len(doc_ids),
    }


async def synthesize_all_topics(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[dict[str, str | int | float | None]]:
    """Synthesize summaries for all topics that have active claims."""
    result = await session.execute(
        text(
            "SELECT DISTINCT c.subject as topic "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE d.tenant_id = :tid AND c.status = 'active' "
            "ORDER BY c.subject"
        ),
        {"tid": tenant_id},
    )
    topics = [r.topic for r in result.fetchall()]

    results = []
    for topic in topics:
        result_item = await synthesize_topic(session, tenant_id, topic)
        results.append(result_item)

    return results
