"""Claim-based contradiction detection.

Compares newly extracted claims against existing claims to find
contradictions — cases where the same predicate has different values.

Example: Claim A says "primary visualization tool = Power BI"
         Claim B says "primary visualization tool = SAP"
         → Contradiction detected.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.models.claim import Claim
from raasoa.models.governance import ConflictCandidate, ReviewTask
from raasoa.providers.base import EmbeddingProvider

logger = logging.getLogger(__name__)


async def detect_claim_conflicts(
    session: AsyncSession,
    document_id: uuid.UUID,
    tenant_id: uuid.UUID,
    new_claims: list[Claim],
    embedding_provider: EmbeddingProvider,
) -> list[ConflictCandidate]:
    """Find contradictions between new claims and existing claims.

    Strategy:
    1. For each new claim, embed the predicate
    2. Find existing claims with similar predicates (embedding similarity)
    3. If predicate is similar but object_value differs → contradiction
    """
    if not new_claims:
        return []

    # Embed predicates of new claims
    new_predicates = [c.predicate for c in new_claims]
    try:
        new_predicate_embeddings = await embedding_provider.embed(new_predicates)
    except Exception as e:
        logger.warning("Failed to embed predicates: %s", e)
        return []

    # Fetch ALL existing claims ONCE (fix N+1 query)
    result = await session.execute(
        text(
            "SELECT cl.id, cl.document_id, cl.subject, cl.predicate, "
            "cl.object_value, cl.evidence_span, cl.confidence, "
            "d.title as doc_title "
            "FROM claims cl "
            "JOIN documents d ON cl.document_id = d.id "
            "WHERE cl.tenant_id = :tid "
            "  AND cl.document_id != :did "
            "  AND cl.status = 'active' "
            "ORDER BY cl.created_at DESC "
            "LIMIT 500"
        ),
        {"tid": tenant_id, "did": document_id},
    )
    existing_claims = result.fetchall()
    if not existing_claims:
        return []

    # Embed ALL existing predicates ONCE
    existing_predicates = [ec.predicate for ec in existing_claims]
    try:
        existing_embeddings = await embedding_provider.embed(existing_predicates)
    except Exception as e:
        logger.warning("Failed to embed existing predicates: %s", e)
        return []

    conflicts: list[ConflictCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, new_claim in enumerate(new_claims):
        emb = new_predicate_embeddings[i]

        # Compare: similar predicate + different value = contradiction
        for j, existing_claim in enumerate(existing_claims):
            # Cosine similarity between predicates
            sim = _cosine_similarity(emb, existing_embeddings[j])

            if sim < 0.7:  # Predicates not similar enough
                continue

            # Check if values actually differ
            if (
                new_claim.object_value.strip().lower()
                == existing_claim.object_value.strip().lower()
            ):
                continue  # Same value, no contradiction

            # Avoid duplicate pairs
            pair_key = (
                min(str(new_claim.id), str(existing_claim.id)),
                max(str(new_claim.id), str(existing_claim.id)),
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            confidence = round(sim * 0.9, 3)  # High similarity = high confidence

            conflict = ConflictCandidate(
                tenant_id=tenant_id,
                document_a_id=document_id,
                document_b_id=existing_claim.document_id,
                conflict_type="claim_contradiction",
                confidence=confidence,
                details={
                    "new_claim": {
                        "subject": new_claim.subject,
                        "predicate": new_claim.predicate,
                        "value": new_claim.object_value,
                        "evidence": new_claim.evidence_span[:200],
                    },
                    "existing_claim": {
                        "subject": existing_claim.subject,
                        "predicate": existing_claim.predicate,
                        "value": existing_claim.object_value,
                        "evidence": existing_claim.evidence_span[:200],
                    },
                    "predicate_similarity": round(sim, 3),
                    "new_doc_id": str(document_id),
                    "existing_doc_title": existing_claim.doc_title,
                },
                status="new",
            )
            session.add(conflict)
            conflicts.append(conflict)

            logger.info(
                "Claim contradiction: '%s=%s' vs '%s=%s' (sim=%.3f)",
                new_claim.predicate, new_claim.object_value,
                existing_claim.predicate, existing_claim.object_value,
                sim,
            )

    # Create review tasks for contradictions
    if conflicts:
        await session.flush()  # Get conflict IDs

        for conflict in conflicts:
            review = ReviewTask(
                tenant_id=tenant_id,
                document_id=document_id,
                conflict_id=conflict.id,
                task_type="conflict_review",
                status="new",
            )
            session.add(review)

        # Update document conflict status
        await session.execute(
            text(
                "UPDATE documents SET conflict_status = 'conflicts_detected' "
                "WHERE id = :did"
            ),
            {"did": document_id},
        )

    logger.info(
        "Found %d claim contradictions for document %s",
        len(conflicts), document_id,
    )
    return conflicts


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
