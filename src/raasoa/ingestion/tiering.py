"""Tiered Indexing: Hot / Warm / Cold.

- Hot:  Full embeddings for all chunks. High-priority, frequently accessed docs.
- Warm: Summary embedding only (first chunk / title). Lower priority docs.
- Cold: BM25 only (tsvector). Archival docs, no embedding cost.

Tiering is based on document access patterns, quality score, and explicit overrides.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.models.document import Document

logger = logging.getLogger(__name__)

# Thresholds for automatic tier assignment
HOT_MIN_QUALITY = 0.7
COLD_MAX_QUALITY = 0.3
COLD_DAYS_SINCE_ACCESS = 90


def assign_initial_tier(doc: Document) -> str:
    """Assign initial tier for a newly ingested document.

    New documents default to "hot" (full embeddings). This can be
    overridden later by the background tiering job.
    """
    return "hot"


async def promote_to_hot(
    session: AsyncSession, document_id: str,
) -> None:
    """Promote a document to hot tier — re-embed all chunks."""
    await session.execute(
        text("UPDATE documents SET index_tier = 'hot' WHERE id = :did"),
        {"did": document_id},
    )
    logger.info("Document %s promoted to hot tier", document_id)


async def demote_to_warm(
    session: AsyncSession, document_id: str,
) -> None:
    """Demote a document to warm tier — keep only first-chunk embedding."""
    await session.execute(
        text("UPDATE documents SET index_tier = 'warm' WHERE id = :did"),
        {"did": document_id},
    )
    # Clear embeddings on non-first chunks to save storage
    await session.execute(
        text(
            "UPDATE chunks SET embedding = NULL "
            "WHERE document_id = :did AND chunk_index > 0"
        ),
        {"did": document_id},
    )
    logger.info("Document %s demoted to warm tier", document_id)


async def demote_to_cold(
    session: AsyncSession, document_id: str,
) -> None:
    """Demote a document to cold tier — remove all embeddings, BM25 only."""
    await session.execute(
        text("UPDATE documents SET index_tier = 'cold' WHERE id = :did"),
        {"did": document_id},
    )
    await session.execute(
        text("UPDATE chunks SET embedding = NULL WHERE document_id = :did"),
        {"did": document_id},
    )
    logger.info("Document %s demoted to cold tier (BM25 only)", document_id)


async def run_tiering_sweep(session: AsyncSession) -> dict:
    """Background job: re-evaluate tiers for all documents.

    Returns stats about how many documents were moved between tiers.
    """
    stats = {"promoted_to_hot": 0, "demoted_to_warm": 0, "demoted_to_cold": 0}

    # Find cold candidates: low quality + not accessed in 90+ days
    cold_candidates = await session.execute(
        text(
            "SELECT id FROM documents "
            "WHERE status = 'indexed' AND index_tier != 'cold' "
            "AND (quality_score IS NOT NULL AND quality_score < :cold_quality) "
            "AND (last_accessed_at IS NULL "
            "     OR last_accessed_at < now() - interval ':cold_days days')"
        ),
        {"cold_quality": COLD_MAX_QUALITY, "cold_days": COLD_DAYS_SINCE_ACCESS},
    )
    for row in cold_candidates.fetchall():
        await demote_to_cold(session, str(row.id))
        stats["demoted_to_cold"] += 1

    # Find warm candidates: medium quality, not recently accessed
    warm_candidates = await session.execute(
        text(
            "SELECT id FROM documents "
            "WHERE status = 'indexed' AND index_tier = 'hot' "
            "AND (quality_score IS NOT NULL AND quality_score < :hot_quality) "
            "AND (last_accessed_at IS NULL "
            "     OR last_accessed_at < now() - interval '30 days')"
        ),
        {"hot_quality": HOT_MIN_QUALITY},
    )
    for row in warm_candidates.fetchall():
        await demote_to_warm(session, str(row.id))
        stats["demoted_to_warm"] += 1

    # Promote recently accessed warm/cold docs back to hot
    hot_candidates = await session.execute(
        text(
            "SELECT id FROM documents "
            "WHERE status = 'indexed' AND index_tier IN ('warm', 'cold') "
            "AND access_count > 5 "
            "AND last_accessed_at > now() - interval '7 days'"
        ),
    )
    for row in hot_candidates.fetchall():
        await promote_to_hot(session, str(row.id))
        stats["promoted_to_hot"] += 1

    await session.commit()
    logger.info("Tiering sweep complete: %s", stats)
    return stats
