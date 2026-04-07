"""Knowledge Index — materialized lookup for factual queries.

Compiles claims into a normalized index that enables sub-millisecond
answers for entity-attribute-value questions. No embedding needed.

The index is the "fast path" in retrieval:
1. Parse query into (subject?, predicate?) pattern
2. Look up in knowledge_index
3. If found → return instantly with 100% confidence
4. If not → fall through to hybrid search

Normalization:
- Lowercase, strip whitespace
- Remove common filler words
- Collapse synonyms (basic)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Filler words to strip during normalization
_FILLER = {
    "the", "a", "an", "of", "for", "in", "to", "and", "or", "is",
    "are", "was", "were", "our", "their", "its", "das", "die", "der",
    "ein", "eine", "und", "oder", "für", "von", "im", "am",
}


def normalize(text_val: str) -> str:
    """Normalize a string for index lookup.

    Lowercase, strip filler words, collapse whitespace.
    """
    words = re.sub(r"[^\w\s]", " ", text_val.lower()).split()
    words = [w for w in words if w not in _FILLER]
    return " ".join(words).strip()


@dataclass
class IndexEntry:
    """A single entry in the knowledge index."""

    subject: str
    predicate: str
    value: str
    confidence: float
    source_documents: list[str]
    status: str = "active"


@dataclass
class IndexLookupResult:
    """Result of a knowledge index lookup."""

    found: bool
    entries: list[IndexEntry]
    query_subject: str | None = None
    query_predicate: str | None = None


async def build_index(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> dict[str, int]:
    """Rebuild the knowledge index from active claims.

    Groups claims by (subject, predicate), picks the highest-confidence
    value, and upserts into the index table.
    """
    # Fetch all active claims grouped by subject + predicate
    result = await session.execute(
        text(
            "SELECT c.subject, c.predicate, c.object_value, "
            "  c.confidence, c.id as claim_id, c.document_id "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE d.tenant_id = :tid "
            "  AND c.status = 'active' "
            "  AND d.review_status NOT IN "
            "    ('quarantined', 'rejected', 'superseded', 'deleted') "
            "ORDER BY c.subject, c.predicate, c.confidence DESC"
        ),
        {"tid": tenant_id},
    )
    claims = result.fetchall()

    if not claims:
        return {"entries": 0, "claims_processed": 0}

    # Group by normalized (subject, predicate)
    groups: dict[tuple[str, str], list[Any]] = {}
    for c in claims:
        key = (normalize(c.subject), normalize(c.predicate))
        if key not in groups:
            groups[key] = []
        groups[key].append(c)

    # Clear existing index for this tenant
    await session.execute(
        text("DELETE FROM knowledge_index WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )

    # Build index entries
    count = 0
    for (subj_norm, pred_norm), group in groups.items():
        # Pick highest-confidence value
        best = group[0]
        claim_ids = [str(c.claim_id) for c in group]
        doc_ids = list({str(c.document_id) for c in group})

        await session.execute(
            text(
                "INSERT INTO knowledge_index "
                "(id, tenant_id, subject, subject_normalized, "
                " predicate, predicate_normalized, value, "
                " source_claim_ids, source_document_ids, "
                " confidence, claim_count, status) "
                "VALUES (:id, :tid, :subj, :subj_n, "
                " :pred, :pred_n, :val, "
                " CAST(:claim_ids AS jsonb), "
                " CAST(:doc_ids AS jsonb), "
                " :conf, :count, 'active')"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "subj": best.subject,
                "subj_n": subj_norm,
                "pred": best.predicate,
                "pred_n": pred_norm,
                "val": best.object_value,
                "claim_ids": json.dumps(claim_ids),
                "doc_ids": json.dumps(doc_ids),
                "conf": best.confidence,
                "count": len(group),
            },
        )
        count += 1

    await session.commit()
    logger.info(
        "Built knowledge index: %d entries from %d claims (tenant %s)",
        count, len(claims), tenant_id,
    )
    return {"entries": count, "claims_processed": len(claims)}


async def lookup(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
) -> IndexLookupResult:
    """Look up a query in the knowledge index.

    Tries to match the query against normalized predicates.
    Returns matching entries sorted by confidence.
    """
    query_normalized = normalize(query)

    if not query_normalized:
        return IndexLookupResult(found=False, entries=[])

    # Strategy 1: Full-text match on predicate
    result = await session.execute(
        text(
            "SELECT subject, predicate, value, confidence, "
            "  source_document_ids, status "
            "FROM knowledge_index "
            "WHERE tenant_id = :tid "
            "  AND status = 'active' "
            "  AND predicate_normalized LIKE :pattern "
            "ORDER BY confidence DESC "
            "LIMIT 5"
        ),
        {"tid": tenant_id, "pattern": f"%{query_normalized}%"},
    )
    rows = result.fetchall()

    if not rows:
        # Strategy 2: Match any word from query against predicates
        words = query_normalized.split()
        if len(words) >= 2:
            # Use the two most significant words
            pattern = "%".join(words[-2:])
            result = await session.execute(
                text(
                    "SELECT subject, predicate, value, confidence, "
                    "  source_document_ids, status "
                    "FROM knowledge_index "
                    "WHERE tenant_id = :tid "
                    "  AND status = 'active' "
                    "  AND predicate_normalized LIKE :pattern "
                    "ORDER BY confidence DESC "
                    "LIMIT 5"
                ),
                {"tid": tenant_id, "pattern": f"%{pattern}%"},
            )
            rows = result.fetchall()

    if not rows:
        return IndexLookupResult(
            found=False, entries=[],
            query_predicate=query_normalized,
        )

    entries = [
        IndexEntry(
            subject=r.subject,
            predicate=r.predicate,
            value=r.value,
            confidence=r.confidence,
            source_documents=r.source_document_ids or [],
            status=r.status,
        )
        for r in rows
    ]

    return IndexLookupResult(
        found=True,
        entries=entries,
        query_predicate=query_normalized,
    )
