import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.schemas.quality import (
    ConflictCandidateResponse,
    ConflictResolution,
    QualityFindingResponse,
    QualityReport,
    ReviewAction,
    ReviewTaskResponse,
)

router = APIRouter(prefix="/v1", tags=["quality"])


@router.get("/documents/{document_id}/quality", response_model=QualityReport)
async def get_document_quality(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> QualityReport:
    """Get quality report for a document."""
    doc_result = await session.execute(
        text(
            "SELECT id, title, quality_score, review_status, conflict_status "
            "FROM documents WHERE id = :did"
        ),
        {"did": document_id},
    )
    doc = doc_result.first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    findings_result = await session.execute(
        text(
            "SELECT id, document_id, finding_type, severity, details, created_at "
            "FROM quality_findings WHERE document_id = :did "
            "ORDER BY created_at"
        ),
        {"did": document_id},
    )

    return QualityReport(
        document_id=doc.id,
        title=doc.title,
        quality_score=doc.quality_score,
        review_status=doc.review_status,
        conflict_status=doc.conflict_status,
        findings=[
            QualityFindingResponse(
                id=f.id, document_id=f.document_id,
                finding_type=f.finding_type, severity=f.severity,
                details=f.details, created_at=f.created_at,
            )
            for f in findings_result.fetchall()
        ],
    )


@router.get("/quality/findings", response_model=list[QualityFindingResponse])
async def list_quality_findings(
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    severity: str | None = Query(default=None),
    finding_type: str | None = Query(default=None),
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[QualityFindingResponse]:
    """List quality findings across all documents."""
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

    # Build dynamic query
    conditions = ["d.tenant_id = :tid"]
    params: dict = {"tid": tenant_id, "lim": limit, "off": offset}

    if severity:
        conditions.append("qf.severity = :severity")
        params["severity"] = severity
    if finding_type:
        conditions.append("qf.finding_type = :finding_type")
        params["finding_type"] = finding_type

    where = " AND ".join(conditions)
    sql = text(
        f"SELECT qf.id, qf.document_id, qf.finding_type, qf.severity, "
        f"qf.details, qf.created_at "
        f"FROM quality_findings qf "
        f"JOIN documents d ON qf.document_id = d.id "
        f"WHERE {where} "
        f"ORDER BY qf.created_at DESC LIMIT :lim OFFSET :off"
    )

    result = await session.execute(sql, params)
    return [
        QualityFindingResponse(
            id=r.id, document_id=r.document_id,
            finding_type=r.finding_type, severity=r.severity,
            details=r.details, created_at=r.created_at,
        )
        for r in result.fetchall()
    ]


@router.get("/conflicts", response_model=list[ConflictCandidateResponse])
async def list_conflicts(
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    status: str | None = Query(default=None),
    conflict_type: str | None = Query(default=None),
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[ConflictCandidateResponse]:
    """List conflict candidates."""
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

    conditions = ["tenant_id = :tid"]
    params: dict = {"tid": tenant_id, "lim": limit, "off": offset}

    if status:
        conditions.append("status = :status")
        params["status"] = status
    if conflict_type:
        conditions.append("conflict_type = :conflict_type")
        params["conflict_type"] = conflict_type

    where = " AND ".join(conditions)
    sql = text(
        f"SELECT id, document_a_id, document_b_id, conflict_type, "
        f"confidence, details, status, created_at "
        f"FROM conflict_candidates "
        f"WHERE {where} "
        f"ORDER BY confidence DESC NULLS LAST, created_at DESC "
        f"LIMIT :lim OFFSET :off"
    )

    result = await session.execute(sql, params)
    return [
        ConflictCandidateResponse(
            id=r.id, document_a_id=r.document_a_id, document_b_id=r.document_b_id,
            conflict_type=r.conflict_type, confidence=r.confidence,
            details=r.details, status=r.status, created_at=r.created_at,
        )
        for r in result.fetchall()
    ]


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    conflict_id: uuid.UUID,
    body: ConflictResolution,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Resolve a conflict candidate.

    Resolution options:
    - "keep_a": Document A is correct, supersede Document B
    - "keep_b": Document B is correct, supersede Document A
    - "keep_both": Both are correct (different context), dismiss conflict
    - "reject_both": Both are incorrect
    """
    result = await session.execute(
        text(
            "SELECT id, document_a_id, document_b_id, status "
            "FROM conflict_candidates WHERE id = :cid"
        ),
        {"cid": conflict_id},
    )
    conflict = result.first()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")

    resolution = body.resolution
    superseded_doc_id = None

    if resolution == "keep_a":
        # Doc A is correct → supersede Doc B
        superseded_doc_id = conflict.document_b_id
    elif resolution == "keep_b":
        # Doc B is correct → supersede Doc A
        superseded_doc_id = conflict.document_a_id
    elif resolution == "reject_both":
        # Both wrong → reject both
        for did in [conflict.document_a_id, conflict.document_b_id]:
            await session.execute(
                text(
                    "UPDATE documents SET review_status = 'rejected' "
                    "WHERE id = :did"
                ),
                {"did": did},
            )
            await session.execute(
                text(
                    "UPDATE claims SET status = 'rejected' "
                    "WHERE document_id = :did"
                ),
                {"did": did},
            )

    # Supersede the losing document
    if superseded_doc_id:
        await session.execute(
            text(
                "UPDATE documents SET review_status = 'superseded' "
                "WHERE id = :did"
            ),
            {"did": superseded_doc_id},
        )
        await session.execute(
            text(
                "UPDATE claims SET status = 'superseded' "
                "WHERE document_id = :did"
            ),
            {"did": superseded_doc_id},
        )

    # Mark conflict as resolved
    await session.execute(
        text(
            "UPDATE conflict_candidates SET status = 'resolved', "
            "details = details || :resolution WHERE id = :cid"
        ),
        {
            "cid": conflict_id,
            "resolution": {
                "resolution": resolution,
                "comment": body.comment,
                "superseded_doc": str(superseded_doc_id) if superseded_doc_id else None,
            },
        },
    )

    # Close related review tasks
    await session.execute(
        text(
            "UPDATE review_tasks SET status = 'approved', "
            "completed_at = now() WHERE conflict_id = :cid AND status = 'new'"
        ),
        {"cid": conflict_id},
    )

    await session.commit()
    return {
        "status": "resolved",
        "conflict_id": str(conflict_id),
        "resolution": resolution,
        "superseded_document": str(superseded_doc_id) if superseded_doc_id else None,
    }


@router.get("/reviews", response_model=list[ReviewTaskResponse])
async def list_reviews(
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    status: str | None = Query(default=None),
    task_type: str | None = Query(default=None),
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[ReviewTaskResponse]:
    """List review tasks."""
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

    conditions = ["tenant_id = :tid"]
    params: dict = {"tid": tenant_id, "lim": limit, "off": offset}

    if status:
        conditions.append("status = :status")
        params["status"] = status
    if task_type:
        conditions.append("task_type = :task_type")
        params["task_type"] = task_type

    where = " AND ".join(conditions)
    sql = text(
        f"SELECT id, document_id, conflict_id, task_type, status, "
        f"assigned_to, created_at, completed_at "
        f"FROM review_tasks WHERE {where} "
        f"ORDER BY created_at DESC LIMIT :lim OFFSET :off"
    )

    result = await session.execute(sql, params)
    return [
        ReviewTaskResponse(
            id=r.id, document_id=r.document_id, conflict_id=r.conflict_id,
            task_type=r.task_type, status=r.status, assigned_to=r.assigned_to,
            created_at=r.created_at, completed_at=r.completed_at,
        )
        for r in result.fetchall()
    ]


@router.post("/reviews/{review_id}/approve")
async def approve_review(
    review_id: uuid.UUID,
    body: ReviewAction,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve a review task and publish the document."""
    result = await session.execute(
        text("SELECT id, document_id, status FROM review_tasks WHERE id = :rid"),
        {"rid": review_id},
    )
    review = result.first()
    if not review:
        raise HTTPException(status_code=404, detail="Review task not found")

    now = datetime.now(UTC)
    await session.execute(
        text("UPDATE review_tasks SET status = 'approved', completed_at = :now WHERE id = :rid"),
        {"rid": review_id, "now": now},
    )

    # Publish the document
    if review.document_id:
        await session.execute(
            text("UPDATE documents SET review_status = 'published' WHERE id = :did"),
            {"did": review.document_id},
        )

    await session.commit()
    return {"status": "approved", "review_id": str(review_id)}


@router.post("/reviews/{review_id}/reject")
async def reject_review(
    review_id: uuid.UUID,
    body: ReviewAction,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reject a review task."""
    result = await session.execute(
        text("SELECT id, document_id FROM review_tasks WHERE id = :rid"),
        {"rid": review_id},
    )
    review = result.first()
    if not review:
        raise HTTPException(status_code=404, detail="Review task not found")

    now = datetime.now(UTC)
    await session.execute(
        text("UPDATE review_tasks SET status = 'rejected', completed_at = :now WHERE id = :rid"),
        {"rid": review_id, "now": now},
    )

    if review.document_id:
        await session.execute(
            text("UPDATE documents SET review_status = 'rejected' WHERE id = :did"),
            {"did": review.document_id},
        )

    await session.commit()
    return {"status": "rejected", "review_id": str(review_id)}
