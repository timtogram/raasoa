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

    # Find documents with overlapping claims. Use trigram similarity
    # (pg_trgm) so that slightly different LLM-extracted predicates
    # still match (e.g. "data retention period" vs "retention period for data").
    related_by_claims = await session.execute(
        text(
            "SELECT DISTINCT ON (d2.id) d2.id AS related_id, d2.title, "
            "  c1.predicate AS shared_predicate, "
            "  c1.object_value AS this_value, "
            "  c2.object_value AS related_value, "
            "  similarity(LOWER(c1.predicate), LOWER(c2.predicate)) AS sim "
            "FROM claims c1 "
            "JOIN claims c2 ON c1.document_id != c2.document_id "
            "  AND (LOWER(c1.predicate) = LOWER(c2.predicate) "
            "       OR similarity(LOWER(c1.predicate), LOWER(c2.predicate)) > 0.45) "
            "JOIN documents d2 ON c2.document_id = d2.id "
            "WHERE c1.document_id = :did "
            "  AND c1.status = 'active' "
            "  AND c2.status = 'active' "
            "  AND d2.tenant_id = :tid "
            "  AND d2.status != 'deleted' "
            "ORDER BY d2.id, sim DESC "
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
            "similarity": float(r.sim) if r.sim is not None else 1.0,
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


@router.get("/dependencies/graph")
async def tenant_dependency_graph(
    request: Request,
    min_similarity: float = 0.5,
    limit_nodes: int = 200,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Tenant-wide dependency graph.

    Returns ``{"nodes": [...], "edges": [...]}`` suitable for graph
    visualization. Edges come from shared claims and conflict candidates.
    """
    tenant_id = await resolve_tenant_async(request)

    # Nodes: all active tenant documents (bounded)
    nodes_result = await session.execute(
        text(
            "SELECT id, title, doc_type, quality_score, review_status, status "
            "FROM documents "
            "WHERE tenant_id = :tid AND status != 'deleted' "
            "ORDER BY created_at DESC LIMIT :lim"
        ),
        {"tid": tenant_id, "lim": limit_nodes},
    )
    nodes = [
        {
            "id": str(r.id),
            "title": r.title,
            "doc_type": r.doc_type,
            "quality": float(r.quality_score) if r.quality_score is not None else None,
            "review_status": r.review_status,
            "status": r.status,
        }
        for r in nodes_result.fetchall()
    ]
    node_ids = {n["id"] for n in nodes}

    # Shared-claim edges (undirected — dedupe by id pair)
    edges_result = await session.execute(
        text(
            "SELECT c1.document_id AS a_id, c2.document_id AS b_id, "
            "       c1.predicate AS pred_a, c2.predicate AS pred_b, "
            "       c1.object_value AS val_a, c2.object_value AS val_b, "
            "       similarity(LOWER(c1.predicate), LOWER(c2.predicate)) AS sim "
            "FROM claims c1 "
            "JOIN claims c2 ON c1.document_id < c2.document_id "
            "  AND (LOWER(c1.predicate) = LOWER(c2.predicate) "
            "       OR similarity(LOWER(c1.predicate), LOWER(c2.predicate)) > :min_sim) "
            "WHERE c1.tenant_id = :tid AND c2.tenant_id = :tid "
            "  AND c1.status = 'active' AND c2.status = 'active' "
            "LIMIT 500"
        ),
        {"tid": tenant_id, "min_sim": min_similarity},
    )
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for r in edges_result.fetchall():
        a, b = str(r.a_id), str(r.b_id)
        if a not in node_ids or b not in node_ids:
            continue
        key = (a, b)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        edges.append({
            "source": a,
            "target": b,
            "type": (
                "contradiction"
                if (r.val_a or "").strip().lower() != (r.val_b or "").strip().lower()
                else "agreement"
            ),
            "predicate": r.pred_a,
            "similarity": float(r.sim) if r.sim is not None else 1.0,
        })

    # Conflict-candidate edges overlay (even stronger signal)
    conflicts_result = await session.execute(
        text(
            "SELECT document_id_a, document_id_b, conflict_type, confidence "
            "FROM conflict_candidates "
            "WHERE tenant_id = :tid AND status IN ('pending', 'confirmed') "
            "LIMIT 500"
        ),
        {"tid": tenant_id},
    )
    for r in conflicts_result.fetchall():
        a, b = str(r.document_id_a), str(r.document_id_b)
        if a not in node_ids or b not in node_ids:
            continue
        edges.append({
            "source": a,
            "target": b,
            "type": "conflict",
            "conflict_type": r.conflict_type,
            "confidence": float(r.confidence) if r.confidence is not None else None,
        })

    return {
        "tenant_id": str(tenant_id),
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
    }
