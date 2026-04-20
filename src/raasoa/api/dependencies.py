"""Document dependency graph — find cross-references between documents.

When Document A mentions concepts from Document B, the agent should
know B exists and how to reach it. This enables multi-hop retrieval.

Dependencies are detected via:
1. Explicit references (URLs, document IDs in text)
2. Shared claims (same subject+predicate across documents)
3. Title mentions (Doc A mentions Doc B's title)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant_async

router = APIRouter(prefix="/v1", tags=["dependencies"])


@router.get("/documents/{document_id}/dependencies")
async def get_dependencies(
    request: Request,
    document_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Find documents related to this one via shared claims or references."""
    tenant_id = await resolve_tenant_async(request)

    # Verify document
    doc = await session.execute(
        text(
            "SELECT id, title FROM documents "
            "WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": document_id, "tid": tenant_id},
    )
    doc_row = doc.first()
    if not doc_row:
        raise HTTPException(status_code=404, detail="Document not found")

    # Find documents with overlapping claims (same predicate)
    related_by_claims = await session.execute(
        text(
            "SELECT DISTINCT d2.id AS related_id, d2.title, "
            "  c1.predicate AS shared_predicate, "
            "  c1.object_value AS this_value, "
            "  c2.object_value AS related_value "
            "FROM claims c1 "
            "JOIN claims c2 ON LOWER(c1.predicate) = LOWER(c2.predicate) "
            "  AND c1.document_id != c2.document_id "
            "JOIN documents d2 ON c2.document_id = d2.id "
            "WHERE c1.document_id = :did "
            "  AND c1.status = 'active' "
            "  AND c2.status = 'active' "
            "  AND d2.tenant_id = :tid "
            "  AND d2.status != 'deleted' "
            "ORDER BY d2.title "
            "LIMIT 20"
        ),
        {"did": document_id, "tid": tenant_id},
    )

    claim_deps = [
        {
            "document_id": str(r.related_id),
            "title": r.title,
            "relationship": "shared_claim",
            "predicate": r.shared_predicate,
            "this_value": r.this_value,
            "related_value": r.related_value,
            "is_contradiction": r.this_value != r.related_value,
        }
        for r in related_by_claims.fetchall()
    ]

    # Find documents from same source (siblings)
    siblings = await session.execute(
        text(
            "SELECT d2.id, d2.title, d2.source_object_id "
            "FROM documents d2 "
            "WHERE d2.source_id = ("
            "  SELECT source_id FROM documents WHERE id = :did"
            ") "
            "AND d2.id != :did "
            "AND d2.tenant_id = :tid "
            "AND d2.status != 'deleted' "
            "ORDER BY d2.title "
            "LIMIT 10"
        ),
        {"did": document_id, "tid": tenant_id},
    )

    sibling_deps = [
        {
            "document_id": str(r.id),
            "title": r.title,
            "relationship": "same_source",
            "source_object_id": r.source_object_id,
        }
        for r in siblings.fetchall()
    ]

    return {
        "document_id": document_id,
        "title": doc_row.title,
        "dependencies": {
            "shared_claims": claim_deps,
            "same_source": sibling_deps,
            "total": len(claim_deps) + len(sibling_deps),
        },
    }
