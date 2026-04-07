"""Retrieval feedback loop — cumulative knowledge improvement.

Inspired by Karpathy's principle: "Every query should enrich the system."

When users mark search results as helpful/unhelpful, we store that signal
and use it to boost future rankings. Over time, the system learns which
chunks are most useful for which types of queries.

Architecture:
  1. User searches → gets results
  2. User rates result (thumbs up/down or explicit score)
  3. Feedback stored in retrieval_feedback table
  4. Future searches boost chunks with positive feedback for similar queries
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class FeedbackSignal:
    """A single feedback signal on a retrieval result."""

    query: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    rating: float  # -1.0 to 1.0 (negative=bad, 0=neutral, positive=good)
    tenant_id: uuid.UUID


async def store_feedback(
    session: AsyncSession,
    signal: FeedbackSignal,
) -> None:
    """Store a retrieval feedback signal."""
    await session.execute(
        text(
            "INSERT INTO retrieval_feedback "
            "(id, tenant_id, query_text, chunk_id, document_id, rating) "
            "VALUES (:id, :tid, :query, :cid, :did, :rating)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": signal.tenant_id,
            "query": signal.query,
            "cid": signal.chunk_id,
            "did": signal.document_id,
            "rating": signal.rating,
        },
    )
    await session.commit()
    logger.info(
        "Stored feedback: query='%s' chunk=%s rating=%.1f",
        signal.query[:50], signal.chunk_id, signal.rating,
    )


async def get_feedback_boost(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    chunk_ids: list[uuid.UUID],
) -> dict[uuid.UUID, float]:
    """Get cumulative feedback scores for chunks.

    Returns a map of chunk_id → boost_score.
    Positive means the chunk has been marked helpful before.
    Used to adjust hybrid search scores.
    """
    if not chunk_ids:
        return {}

    result = await session.execute(
        text(
            "SELECT chunk_id, "
            "  SUM(rating) as total_rating, "
            "  COUNT(*) as feedback_count "
            "FROM retrieval_feedback "
            "WHERE tenant_id = :tid "
            "  AND chunk_id = ANY(:cids) "
            "GROUP BY chunk_id"
        ),
        {"tid": tenant_id, "cids": chunk_ids},
    )

    boosts: dict[uuid.UUID, float] = {}
    for row in result.fetchall():
        # Normalize: avg rating weighted by log(count+1)
        import math
        avg = row.total_rating / row.feedback_count
        weight = math.log(row.feedback_count + 1)
        boosts[row.chunk_id] = avg * weight * 0.1  # Scale factor
    return boosts
