"""Prometheus-compatible metrics endpoint.

Exposes key operational metrics in text/plain format for scraping.
No external dependency required — generates the format directly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def prometheus_metrics(
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Prometheus metrics endpoint — no auth required for scraping."""
    from fastapi.responses import PlainTextResponse

    lines: list[str] = []

    try:
        result = await session.execute(text(
            "SELECT "
            "(SELECT COUNT(*) FROM documents WHERE status != 'deleted') "
            "  as total_docs, "
            "(SELECT COUNT(*) FROM documents WHERE status = 'indexed') "
            "  as indexed_docs, "
            "(SELECT COUNT(*) FROM documents "
            "  WHERE review_status = 'quarantined') as quarantined, "
            "(SELECT COUNT(*) FROM chunks) as total_chunks, "
            "(SELECT COUNT(*) FROM claims WHERE status = 'active') "
            "  as active_claims, "
            "(SELECT COUNT(*) FROM claims WHERE status = 'superseded') "
            "  as superseded_claims, "
            "(SELECT COUNT(*) FROM conflict_candidates "
            "  WHERE status = 'new') as open_conflicts, "
            "(SELECT COUNT(*) FROM review_tasks "
            "  WHERE status = 'new') as pending_reviews, "
            "(SELECT COUNT(*) FROM knowledge_index) as index_entries, "
            "(SELECT COUNT(*) FROM retrieval_feedback) as feedback_count, "
            "(SELECT COALESCE(AVG(quality_score), 0) "
            "  FROM documents WHERE quality_score IS NOT NULL) "
            "  as avg_quality, "
            "(SELECT COALESCE(AVG(latency_ms), 0) "
            "  FROM retrieval_logs "
            "  WHERE created_at > now() - interval '1 hour') "
            "  as avg_retrieval_latency_ms"
        ))
        row = result.first()

        if row:
            lines.extend([
                "# HELP raasoa_documents_total Total documents",
                "# TYPE raasoa_documents_total gauge",
                f"raasoa_documents_total {row.total_docs}",
                "",
                "# HELP raasoa_documents_indexed Indexed documents",
                "# TYPE raasoa_documents_indexed gauge",
                f"raasoa_documents_indexed {row.indexed_docs}",
                "",
                "# HELP raasoa_documents_quarantined Quarantined documents",
                "# TYPE raasoa_documents_quarantined gauge",
                f"raasoa_documents_quarantined {row.quarantined}",
                "",
                "# HELP raasoa_chunks_total Total chunks",
                "# TYPE raasoa_chunks_total gauge",
                f"raasoa_chunks_total {row.total_chunks}",
                "",
                "# HELP raasoa_claims_active Active claims",
                "# TYPE raasoa_claims_active gauge",
                f"raasoa_claims_active {row.active_claims}",
                "",
                "# HELP raasoa_claims_superseded Superseded claims",
                "# TYPE raasoa_claims_superseded gauge",
                f"raasoa_claims_superseded {row.superseded_claims}",
                "",
                "# HELP raasoa_conflicts_open Open conflicts",
                "# TYPE raasoa_conflicts_open gauge",
                f"raasoa_conflicts_open {row.open_conflicts}",
                "",
                "# HELP raasoa_reviews_pending Pending reviews",
                "# TYPE raasoa_reviews_pending gauge",
                f"raasoa_reviews_pending {row.pending_reviews}",
                "",
                "# HELP raasoa_knowledge_index_entries Knowledge index entries",
                "# TYPE raasoa_knowledge_index_entries gauge",
                f"raasoa_knowledge_index_entries {row.index_entries}",
                "",
                "# HELP raasoa_feedback_total Total feedback signals",
                "# TYPE raasoa_feedback_total counter",
                f"raasoa_feedback_total {row.feedback_count}",
                "",
                "# HELP raasoa_quality_average Average quality score",
                "# TYPE raasoa_quality_average gauge",
                f"raasoa_quality_average {row.avg_quality:.3f}",
                "",
                "# HELP raasoa_retrieval_latency_ms Avg retrieval latency (1h)",
                "# TYPE raasoa_retrieval_latency_ms gauge",
                f"raasoa_retrieval_latency_ms {row.avg_retrieval_latency_ms:.0f}",
            ])
    except Exception:
        lines.append("# Error collecting metrics")

    return PlainTextResponse("\n".join(lines) + "\n")
