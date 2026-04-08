import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class QualityFindingResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    finding_type: str
    severity: str
    details: dict[str, Any] | None
    created_at: datetime


class QualityReport(BaseModel):
    document_id: uuid.UUID
    title: str | None
    quality_score: float | None
    review_status: str
    conflict_status: str
    findings: list[QualityFindingResponse]


class ConflictCandidateResponse(BaseModel):
    id: uuid.UUID
    document_a_id: uuid.UUID
    document_b_id: uuid.UUID
    conflict_type: str
    confidence: float | None
    details: dict[str, Any] | None
    status: str
    created_at: datetime


class ReviewTaskResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID | None
    conflict_id: uuid.UUID | None
    task_type: str
    status: str
    assigned_to: str | None
    created_at: datetime
    completed_at: datetime | None


class ConflictResolution(BaseModel):
    resolution: str = Field(
        ...,
        description=(
            "keep_a, keep_b, keep_both, reject_both, dismiss. "
            "When keep_both: use context_a/context_b to explain WHY both are valid."
        ),
    )
    comment: str = ""
    context_a: str | None = Field(
        default=None,
        description="When keep_both: context for Doc A (e.g. 'applies to Marketing dept')",
    )
    context_b: str | None = Field(
        default=None,
        description="When keep_both: context for Doc B (e.g. 'applies to Engineering dept')",
    )


class ReviewAction(BaseModel):
    comment: str = ""
