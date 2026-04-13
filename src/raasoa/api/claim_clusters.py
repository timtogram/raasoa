"""Claim Cluster API — group conflicting claims across all documents.

Instead of pairwise A↔B conflicts, this groups ALL claims about
the same topic and shows where knowledge diverges.

Example:
  Topic: "primary BI tool"
  Claims:
    - Doc A (2024): "Power BI"         ← superseded
    - Doc B (2025): "SAP Analytics"    ← active
    - Doc C (2026): "Looker"           ← active, conflicts with B

The cluster view makes it obvious: 3 documents disagree,
Doc A is already superseded, but B and C still conflict.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant_async

router = APIRouter(prefix="/v1", tags=["claims"])


@router.get("/claim-clusters")
async def list_claim_clusters(
    request: Request,
    min_variants: int = 2,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Find topics where multiple documents disagree.

    Groups claims by normalized predicate, returns clusters where
    there are at least `min_variants` different values.
    """
    tenant_id = await resolve_tenant_async(request)

    result = await session.execute(
        text(
            "WITH claim_groups AS ("
            "  SELECT "
            "    LOWER(TRIM(c.predicate)) AS predicate_norm, "
            "    c.object_value, "
            "    c.confidence, "
            "    c.status, "
            "    c.valid_from, "
            "    c.valid_until, "
            "    c.document_id, "
            "    d.title AS doc_title, "
            "    d.review_status AS doc_status, "
            "    s.name AS source_name "
            "  FROM claims c "
            "  JOIN documents d ON c.document_id = d.id "
            "  LEFT JOIN sources s ON d.source_id = s.id "
            "  WHERE d.tenant_id = :tid "
            ") "
            "SELECT predicate_norm, "
            "  COUNT(DISTINCT object_value) AS variant_count, "
            "  COUNT(*) AS total_claims, "
            "  jsonb_agg(jsonb_build_object("
            "    'value', object_value, "
            "    'confidence', confidence, "
            "    'status', status, "
            "    'valid_from', valid_from, "
            "    'valid_until', valid_until, "
            "    'document_id', document_id, "
            "    'doc_title', doc_title, "
            "    'doc_status', doc_status, "
            "    'source_name', source_name"
            "  ) ORDER BY confidence DESC) AS claims "
            "FROM claim_groups "
            "GROUP BY predicate_norm "
            "HAVING COUNT(DISTINCT object_value) >= :min_variants "
            "ORDER BY COUNT(DISTINCT object_value) DESC, "
            "  predicate_norm"
        ),
        {"tid": tenant_id, "min_variants": min_variants},
    )

    clusters = []
    for row in result.fetchall():
        claims_list = row.claims if row.claims else []

        # Deduplicate by value — show unique positions
        seen_values: dict[str, dict[str, Any]] = {}
        for c in claims_list:
            val = c.get("value", "")
            if val not in seen_values or (c.get("confidence") or 0) > (
                seen_values[val].get("confidence") or 0
            ):
                seen_values[val] = c

        clusters.append({
            "predicate": row.predicate_norm,
            "variant_count": row.variant_count,
            "total_claims": row.total_claims,
            "variants": list(seen_values.values()),
        })

    return clusters


@router.get("/claim-clusters/{predicate}")
async def get_cluster_detail(
    request: Request,
    predicate: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get all claims for a specific predicate across all documents."""
    tenant_id = await resolve_tenant_async(request)

    result = await session.execute(
        text(
            "SELECT c.id, c.subject, c.predicate, c.object_value, "
            "  c.confidence, c.status, c.valid_from, c.valid_until, "
            "  c.evidence_span, "
            "  d.id AS doc_id, d.title AS doc_title, "
            "  d.review_status AS doc_status, "
            "  s.name AS source_name, s.source_type "
            "FROM claims c "
            "JOIN documents d ON c.document_id = d.id "
            "LEFT JOIN sources s ON d.source_id = s.id "
            "WHERE d.tenant_id = :tid "
            "  AND LOWER(TRIM(c.predicate)) = :pred "
            "ORDER BY c.confidence DESC"
        ),
        {"tid": tenant_id, "pred": predicate.lower().strip()},
    )

    claims = [
        {
            "claim_id": str(r.id),
            "subject": r.subject,
            "predicate": r.predicate,
            "value": r.object_value,
            "confidence": r.confidence,
            "status": r.status,
            "valid_from": r.valid_from,
            "valid_until": r.valid_until,
            "evidence": r.evidence_span[:200] if r.evidence_span else None,
            "document_id": str(r.doc_id),
            "document_title": r.doc_title,
            "document_status": r.doc_status,
            "source_name": r.source_name,
            "source_type": r.source_type,
        }
        for r in result.fetchall()
    ]

    return {
        "predicate": predicate,
        "total_claims": len(claims),
        "claims": claims,
    }
