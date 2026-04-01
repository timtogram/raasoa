"""Quality gate orchestrator — runs checks, scores, persists findings."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.ingestion.chunker import ChunkResult
from raasoa.ingestion.parser import ParsedDocument
from raasoa.models.document import Document
from raasoa.models.governance import QualityFinding, ReviewTask
from raasoa.quality.checks import FindingResult, run_all_checks
from raasoa.quality.duplicate import check_chunk_overlap, check_exact_duplicate
from raasoa.quality.scorer import QualityAssessment, compute_quality_score


async def run_quality_gate(
    session: AsyncSession,
    doc: Document,
    parsed: ParsedDocument,
    chunks: list[ChunkResult],
    embedded_count: int,
    chunk_hashes: list[bytes] | None = None,
) -> QualityAssessment:
    """Run all quality checks and update document accordingly.

    This is the main entry point called from the ingestion pipeline.
    """
    # 1. Run rule-based checks (pure functions, no DB)
    findings = run_all_checks(parsed, chunks, embedded_count)

    # 2. Check for exact duplicates (DB query)
    if doc.content_hash:
        dup = await check_exact_duplicate(
            session, doc.tenant_id, doc.content_hash, exclude_doc_id=doc.id
        )
        if dup:
            findings.append(
                FindingResult(
                    finding_type="exact_duplicate",
                    severity="warning",
                    details={
                        "duplicate_document_id": str(dup.document_id),
                        "duplicate_title": dup.title,
                    },
                    score_penalty=0.15,
                )
            )

    # 3. Check for chunk overlap with existing documents
    if chunk_hashes:
        overlaps = await check_chunk_overlap(
            session, doc.tenant_id, chunk_hashes, exclude_doc_id=doc.id
        )
        for overlap in overlaps:
            if overlap.overlap_ratio > settings.conflict_overlap_threshold:
                findings.append(
                    FindingResult(
                        finding_type="high_chunk_overlap",
                        severity="warning",
                        details={
                            "overlapping_document_id": str(overlap.document_id),
                            "overlapping_title": overlap.title,
                            "overlapping_chunks": overlap.overlapping_chunks,
                            "overlap_ratio": overlap.overlap_ratio,
                        },
                        score_penalty=0.1,
                    )
                )

    # 4. Compute aggregate score and decision
    assessment = compute_quality_score(findings)

    # 5. Persist findings to DB
    for f in findings:
        finding = QualityFinding(
            document_id=doc.id,
            finding_type=f.finding_type,
            severity=f.severity,
            details=f.details,
        )
        session.add(finding)

    # 6. Update document
    doc.quality_score = assessment.quality_score
    doc.review_status = assessment.publish_decision

    # 7. Create review task if needed
    if assessment.publish_decision in ("needs_review", "quarantined"):
        review = ReviewTask(
            tenant_id=doc.tenant_id,
            document_id=doc.id,
            task_type="quality_review",
            status="new",
        )
        session.add(review)

    return assessment
