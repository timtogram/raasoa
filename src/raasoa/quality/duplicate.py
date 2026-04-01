"""Duplicate and overlap detection via database queries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class DuplicateMatch:
    document_id: uuid.UUID
    title: str | None


@dataclass
class OverlapMatch:
    document_id: uuid.UUID
    title: str | None
    overlapping_chunks: int
    total_chunks: int
    overlap_ratio: float


async def check_exact_duplicate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    content_hash: bytes,
    exclude_doc_id: uuid.UUID | None = None,
) -> DuplicateMatch | None:
    """Check if a document with the same content hash already exists."""
    sql = text(
        "SELECT id, title FROM documents "
        "WHERE tenant_id = :tid AND content_hash = :hash AND id != :exclude_id "
        "LIMIT 1"
    )
    result = await session.execute(
        sql,
        {
            "tid": tenant_id,
            "hash": content_hash,
            "exclude_id": exclude_doc_id or uuid.UUID(int=0),
        },
    )
    row = result.first()
    if row:
        return DuplicateMatch(document_id=row.id, title=row.title)
    return None


async def check_chunk_overlap(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    chunk_hashes: list[bytes],
    exclude_doc_id: uuid.UUID,
) -> list[OverlapMatch]:
    """Find documents that share chunks with the same content hashes."""
    if not chunk_hashes:
        return []

    sql = text(
        "SELECT c.document_id, d.title, COUNT(*) as overlap_count, d.chunk_count "
        "FROM chunks c "
        "JOIN documents d ON c.document_id = d.id "
        "WHERE d.tenant_id = :tid "
        "  AND c.document_id != :exclude_id "
        "  AND c.content_hash = ANY(:hashes) "
        "GROUP BY c.document_id, d.title, d.chunk_count "
        "HAVING COUNT(*) > 1 "
        "ORDER BY COUNT(*) DESC "
        "LIMIT 10"
    )
    result = await session.execute(
        sql,
        {
            "tid": tenant_id,
            "exclude_id": exclude_doc_id,
            "hashes": chunk_hashes,
        },
    )

    matches: list[OverlapMatch] = []
    for row in result.fetchall():
        total = max(row.chunk_count, 1)
        matches.append(
            OverlapMatch(
                document_id=row.document_id,
                title=row.title,
                overlapping_chunks=row.overlap_count,
                total_chunks=total,
                overlap_ratio=round(row.overlap_count / total, 3),
            )
        )
    return matches
