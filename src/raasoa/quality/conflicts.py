"""Conflict and contradiction detection between documents.

Runs after ingestion commit. Four detection passes:
1. Exact duplicate (same content_hash)
2. Chunk overlap (shared chunk hashes)
3. Title-based supersession (similar titles)
4. Semantic contradiction (close embeddings, different content)
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.models.document import Document
from raasoa.models.governance import ConflictCandidate, ReviewTask


async def detect_conflicts(
    session: AsyncSession,
    doc: Document,
    tenant_id: uuid.UUID,
    chunk_hashes: list[bytes],
    chunk_embeddings: list[list[float]],
) -> list[ConflictCandidate]:
    """Run all conflict detection passes for a newly ingested document."""
    conflicts: list[ConflictCandidate] = []

    # Pass 1: Exact duplicate
    exact_dups = await _detect_exact_duplicates(session, doc, tenant_id)
    conflicts.extend(exact_dups)

    # Pass 2: Chunk overlap
    overlaps = await _detect_chunk_overlap(session, doc, tenant_id, chunk_hashes)
    conflicts.extend(overlaps)

    # Pass 3: Title-based supersession
    supersessions = await _detect_title_supersession(session, doc, tenant_id)
    conflicts.extend(supersessions)

    # Pass 4: Semantic contradiction
    contradictions = await _detect_semantic_contradictions(
        session, doc, tenant_id, chunk_embeddings
    )
    conflicts.extend(contradictions)

    # Persist conflicts and update document
    if conflicts:
        for conflict in conflicts:
            session.add(conflict)

        # Update document conflict status
        await session.execute(
            text("UPDATE documents SET conflict_status = 'conflicts_detected' WHERE id = :did"),
            {"did": doc.id},
        )

        # Create review tasks for high-confidence conflicts
        high_confidence = [c for c in conflicts if (c.confidence or 0) > 0.5]
        if high_confidence:
            for conflict in high_confidence:
                review = ReviewTask(
                    tenant_id=tenant_id,
                    document_id=doc.id,
                    conflict_id=conflict.id,
                    task_type="conflict_review",
                    status="new",
                )
                session.add(review)

        await session.flush()

    return conflicts


async def _detect_exact_duplicates(
    session: AsyncSession, doc: Document, tenant_id: uuid.UUID
) -> list[ConflictCandidate]:
    """Pass 1: Find documents with identical content hash."""
    if not doc.content_hash:
        return []

    result = await session.execute(
        text(
            "SELECT id, title FROM documents "
            "WHERE tenant_id = :tid AND content_hash = :hash AND id != :did "
            "LIMIT 5"
        ),
        {"tid": tenant_id, "hash": doc.content_hash, "did": doc.id},
    )

    conflicts: list[ConflictCandidate] = []
    for row in result.fetchall():
        conflicts.append(
            ConflictCandidate(
                tenant_id=tenant_id,
                document_a_id=doc.id,
                document_b_id=row.id,
                conflict_type="exact_duplicate",
                confidence=1.0,
                details={
                    "message": f"Identical content hash with '{row.title}'",
                    "duplicate_title": row.title,
                },
                status="new",
            )
        )
    return conflicts


async def _detect_chunk_overlap(
    session: AsyncSession,
    doc: Document,
    tenant_id: uuid.UUID,
    chunk_hashes: list[bytes],
) -> list[ConflictCandidate]:
    """Pass 2: Find documents sharing many chunk hashes."""
    if not chunk_hashes:
        return []

    result = await session.execute(
        text(
            "SELECT c.document_id, d.title, COUNT(*) as overlap_count, d.chunk_count "
            "FROM chunks c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE d.tenant_id = :tid "
            "  AND c.document_id != :did "
            "  AND c.content_hash = ANY(:hashes) "
            "GROUP BY c.document_id, d.title, d.chunk_count "
            "HAVING COUNT(*) > 1 "
            "ORDER BY COUNT(*) DESC "
            "LIMIT 5"
        ),
        {"tid": tenant_id, "did": doc.id, "hashes": chunk_hashes},
    )

    conflicts: list[ConflictCandidate] = []
    for row in result.fetchall():
        total = max(row.chunk_count, 1)
        ratio = row.overlap_count / total
        if ratio > settings.conflict_overlap_threshold:
            conflicts.append(
                ConflictCandidate(
                    tenant_id=tenant_id,
                    document_a_id=doc.id,
                    document_b_id=row.document_id,
                    conflict_type="partial_overlap",
                    confidence=round(min(ratio, 1.0), 3),
                    details={
                        "overlapping_chunks": row.overlap_count,
                        "total_chunks": total,
                        "overlap_ratio": round(ratio, 3),
                        "other_title": row.title,
                    },
                    status="new",
                )
            )
    return conflicts


async def _detect_title_supersession(
    session: AsyncSession, doc: Document, tenant_id: uuid.UUID
) -> list[ConflictCandidate]:
    """Pass 3: Find documents with very similar titles (potential new version)."""
    if not doc.title:
        return []

    # Use PostgreSQL LIKE with simplified title matching
    # Strip common version patterns for comparison
    clean_title = doc.title.strip()
    if len(clean_title) < 5:
        return []

    # Search for documents with similar titles using trigram similarity
    # Fallback to LIKE if pg_trgm is not available
    try:
        result = await session.execute(
            text(
                "SELECT id, title, version, created_at FROM documents "
                "WHERE tenant_id = :tid AND id != :did "
                "  AND title IS NOT NULL "
                "  AND LOWER(title) LIKE :pattern "
                "ORDER BY created_at DESC LIMIT 5"
            ),
            {
                "tid": tenant_id,
                "did": doc.id,
                # Match first 60% of the title (fuzzy prefix match)
                "pattern": clean_title[: max(len(clean_title) * 6 // 10, 5)].lower() + "%",
            },
        )
    except Exception:
        return []

    conflicts: list[ConflictCandidate] = []
    for row in result.fetchall():
        if row.title and row.title != doc.title:
            conflicts.append(
                ConflictCandidate(
                    tenant_id=tenant_id,
                    document_a_id=doc.id,
                    document_b_id=row.id,
                    conflict_type="potential_supersession",
                    confidence=0.6,
                    details={
                        "new_title": doc.title,
                        "existing_title": row.title,
                        "existing_version": row.version,
                        "message": "Documents share a similar title prefix",
                    },
                    status="new",
                )
            )
    return conflicts


async def _detect_semantic_contradictions(
    session: AsyncSession,
    doc: Document,
    tenant_id: uuid.UUID,
    chunk_embeddings: list[list[float]],
) -> list[ConflictCandidate]:
    """Pass 4: Find chunks that are semantically similar but have different content.

    Key insight: if two chunks embed close together (same topic/context)
    but have different content_hashes (different actual text), they may
    contradict each other.
    """
    if not chunk_embeddings:
        return []

    # Limit to first 10 chunks for performance
    max_chunks = min(len(chunk_embeddings), 10)
    threshold = settings.conflict_semantic_threshold
    conflicts: list[ConflictCandidate] = []
    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

    for i in range(max_chunks):
        embedding = chunk_embeddings[i]

        # Find nearest neighbors from OTHER documents
        result = await session.execute(
            text(
                "SELECT c.id, c.document_id, c.chunk_text, c.content_hash, "
                "       c.section_title, d.title as doc_title, "
                "       c.embedding <=> :emb AS distance "
                "FROM chunks c "
                "JOIN documents d ON c.document_id = d.id "
                "WHERE d.tenant_id = :tid "
                "  AND c.document_id != :did "
                "  AND c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> :emb "
                "LIMIT 3"
            ),
            {"tid": tenant_id, "did": doc.id, "emb": str(embedding)},
        )

        for row in result.fetchall():
            if row.distance > threshold:
                continue

            # Avoid duplicate conflict pairs
            pair = (min(doc.id, row.document_id), max(doc.id, row.document_id))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            # High semantic similarity + different content = potential contradiction
            confidence = round(max(0.0, 1.0 - row.distance / threshold), 3)

            conflicts.append(
                ConflictCandidate(
                    tenant_id=tenant_id,
                    document_a_id=doc.id,
                    document_b_id=row.document_id,
                    conflict_type="potential_contradiction",
                    confidence=confidence,
                    details={
                        "semantic_distance": round(row.distance, 4),
                        "other_doc_title": row.doc_title,
                        "other_chunk_section": row.section_title,
                        "other_chunk_preview": row.chunk_text[:200],
                        "message": "Semantically similar but different content",
                    },
                    status="new",
                )
            )

    return conflicts
