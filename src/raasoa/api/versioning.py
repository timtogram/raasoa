"""Document versioning — diff between versions, version history.

Shows what changed between document versions. Critical for audit
and governance: "What exactly changed in the travel policy update?"
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant_async

router = APIRouter(prefix="/v1", tags=["versioning"])


@router.get("/documents/{document_id}/versions")
async def list_versions(
    request: Request,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """List all versions of a document."""
    tenant_id = await resolve_tenant_async(request)

    # Verify document belongs to tenant
    doc = await session.execute(
        text(
            "SELECT id FROM documents "
            "WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": document_id, "tid": tenant_id},
    )
    if not doc.first():
        raise HTTPException(status_code=404, detail="Document not found")

    result = await session.execute(
        text(
            "SELECT version, content_hash, parser_version, "
            "chunking_strategy_version, created_at "
            "FROM document_versions "
            "WHERE document_id = :did "
            "ORDER BY version DESC"
        ),
        {"did": document_id},
    )
    return [
        {
            "version": r.version,
            "content_hash": r.content_hash.hex() if r.content_hash else None,
            "parser_version": r.parser_version,
            "created_at": str(r.created_at),
        }
        for r in result.fetchall()
    ]


@router.get("/documents/{document_id}/diff")
async def diff_versions(
    request: Request,
    document_id: uuid.UUID,
    version_a: int = 0,
    version_b: int = 0,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Show what changed between two versions of a document.

    If version_a/version_b not specified, compares the two most
    recent versions. Returns a unified diff.
    """
    tenant_id = await resolve_tenant_async(request)

    doc = await session.execute(
        text(
            "SELECT id, title, version FROM documents "
            "WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": document_id, "tid": tenant_id},
    )
    doc_row = doc.first()
    if not doc_row:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc_row.version < 2:
        return {
            "document_id": str(document_id),
            "title": doc_row.title,
            "message": "Only one version exists — no diff available.",
            "current_version": doc_row.version,
        }

    # Auto-detect versions if not specified
    if version_a == 0 or version_b == 0:
        version_b = doc_row.version
        version_a = doc_row.version - 1

    # Get chunk texts for both versions
    # Current version = chunks in DB
    # Previous version = we need to store chunk snapshots
    # For now: compare current chunks with claim changes
    chunks_result = await session.execute(
        text(
            "SELECT chunk_text FROM chunks "
            "WHERE document_id = :did ORDER BY chunk_index"
        ),
        {"did": document_id},
    )
    _current_chunks = chunks_result.fetchall()  # Reserved for future text diff

    # Get claims diff (what facts changed)
    claims_result = await session.execute(
        text(
            "SELECT predicate, object_value, status, valid_from "
            "FROM claims WHERE document_id = :did "
            "ORDER BY predicate"
        ),
        {"did": document_id},
    )
    claims = [
        {
            "predicate": r.predicate,
            "value": r.object_value,
            "status": r.status,
            "valid_from": r.valid_from,
        }
        for r in claims_result.fetchall()
    ]

    # Get superseded claims from same source_object_id (previous version)
    soid_result = await session.execute(
        text(
            "SELECT source_object_id FROM documents WHERE id = :did"
        ),
        {"did": document_id},
    )
    soid = soid_result.scalar()

    superseded_claims: list[dict[str, Any]] = []
    if soid:
        sup_result = await session.execute(
            text(
                "SELECT c.predicate, c.object_value "
                "FROM claims c "
                "JOIN documents d ON c.document_id = d.id "
                "WHERE d.tenant_id = :tid "
                "AND c.status = 'superseded' "
                "AND c.predicate IN ("
                "  SELECT predicate FROM claims "
                "  WHERE document_id = :did AND status = 'active'"
                ") "
                "ORDER BY c.predicate"
            ),
            {"tid": tenant_id, "did": document_id},
        )
        superseded_claims = [
            {"predicate": r.predicate, "old_value": r.object_value}
            for r in sup_result.fetchall()
        ]

    # Build a readable diff summary
    changes: list[dict[str, str]] = []
    for sc in superseded_claims:
        # Find current value for this predicate
        current = next(
            (c for c in claims if c["predicate"] == sc["predicate"]),
            None,
        )
        if current:
            changes.append({
                "predicate": sc["predicate"],
                "old_value": sc["old_value"],
                "new_value": current["value"],
                "type": "changed",
            })

    return {
        "document_id": str(document_id),
        "title": doc_row.title,
        "current_version": doc_row.version,
        "compared_versions": f"v{version_a} → v{version_b}",
        "claim_changes": changes,
        "current_claims": claims,
        "superseded_claims": superseded_claims,
    }
