"""Source tree API — hierarchical view with quality overlay.

Shows documents grouped by source, with aggregated quality scores
and conflict counts per source/folder.

Use case: "SharePoint/Policies has 95% quality, but Archive/ has 40%
and 12 conflicts."
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant_async

router = APIRouter(prefix="/v1", tags=["source-tree"])


@router.get("/source-tree")
async def source_tree(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Hierarchical source view with quality overlay.

    Returns sources with their documents, quality scores, and
    conflict counts — organized as a tree.
    """
    tenant_id = await resolve_tenant_async(request)

    # Source-level aggregation
    result = await session.execute(
        text(
            "SELECT "
            "  s.id AS source_id, "
            "  s.name AS source_name, "
            "  s.source_type, "
            "  COUNT(d.id) AS doc_count, "
            "  COUNT(d.id) FILTER ("
            "    WHERE d.status = 'indexed'"
            "  ) AS indexed_count, "
            "  COUNT(d.id) FILTER ("
            "    WHERE d.review_status = 'quarantined'"
            "  ) AS quarantined_count, "
            "  COUNT(d.id) FILTER ("
            "    WHERE d.conflict_status = 'conflicts_detected'"
            "  ) AS conflicted_count, "
            "  ROUND(AVG(d.quality_score)::numeric, 3) "
            "    AS avg_quality, "
            "  ROUND(MIN(d.quality_score)::numeric, 3) "
            "    AS min_quality, "
            "  SUM(d.chunk_count) AS total_chunks, "
            "  (SELECT COUNT(*) FROM claims c "
            "   WHERE c.document_id = ANY(ARRAY_AGG(d.id)) "
            "   AND c.status = 'active') AS active_claims "
            "FROM sources s "
            "LEFT JOIN documents d "
            "  ON d.source_id = s.id AND d.status != 'deleted' "
            "WHERE s.tenant_id = :tid "
            "GROUP BY s.id, s.name, s.source_type "
            "ORDER BY s.name"
        ),
        {"tid": tenant_id},
    )

    sources = []
    for r in result.fetchall():
        # Get documents for this source
        docs_result = await session.execute(
            text(
                "SELECT d.id, d.title, d.quality_score, "
                "  d.review_status, d.conflict_status, "
                "  d.chunk_count, d.source_url, "
                "  d.source_object_id, d.doc_metadata, "
                "  (SELECT COUNT(*) FROM claims c "
                "   WHERE c.document_id = d.id "
                "   AND c.status = 'active') AS claim_count "
                "FROM documents d "
                "WHERE d.source_id = :sid "
                "  AND d.status != 'deleted' "
                "ORDER BY d.title"
            ),
            {"sid": r.source_id},
        )

        # Build folder tree from source_object_ids
        documents = []
        folders: dict[str, dict[str, Any]] = {}

        for doc in docs_result.fetchall():
            path = doc.source_object_id or ""
            metadata = doc.doc_metadata or {}
            source_path = (
                metadata.get("source_path")
                or metadata.get("path")
                or path
            )
            folder = metadata.get("folder_path") or ""
            if not folder and "/" in source_path:
                folder = "/".join(source_path.split("/")[:-1])

            doc_entry = {
                "id": str(doc.id),
                "title": doc.title,
                "quality_score": (
                    float(doc.quality_score)
                    if doc.quality_score is not None
                    else None
                ),
                "review_status": doc.review_status,
                "conflict_status": doc.conflict_status,
                "chunk_count": doc.chunk_count,
                "claim_count": doc.claim_count,
                "source_url": doc.source_url,
                "source_path": source_path,
                "folder": folder or "(root)",
            }
            documents.append(doc_entry)

            # Aggregate per folder
            if folder not in folders:
                folders[folder or "(root)"] = {
                    "name": folder or "(root)",
                    "doc_count": 0,
                    "quality_scores": [],
                    "conflict_count": 0,
                }
            f = folders[folder or "(root)"]
            f["doc_count"] += 1
            if doc.quality_score is not None:
                f["quality_scores"].append(float(doc.quality_score))
            if doc.conflict_status == "conflicts_detected":
                f["conflict_count"] += 1

        # Finalize folder averages
        folder_list = []
        for f in folders.values():
            scores = f.pop("quality_scores")
            f["avg_quality"] = (
                round(sum(scores) / len(scores), 3) if scores else None
            )
            folder_list.append(f)

        sources.append({
            "source_id": str(r.source_id),
            "source_name": r.source_name,
            "source_type": r.source_type,
            "summary": {
                "doc_count": r.doc_count,
                "indexed_count": r.indexed_count,
                "quarantined_count": r.quarantined_count,
                "conflicted_count": r.conflicted_count,
                "avg_quality": (
                    float(r.avg_quality) if r.avg_quality is not None else None
                ),
                "min_quality": (
                    float(r.min_quality) if r.min_quality is not None else None
                ),
                "total_chunks": r.total_chunks,
                "active_claims": r.active_claims,
            },
            "folders": sorted(
                folder_list, key=lambda x: x["name"],
            ),
            "documents": documents,
        })

    return sources
