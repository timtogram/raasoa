import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class QualityFindingResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    finding_type: str
    severity: str
    details: dict | None
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
    details: dict | None
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
    resolution: str = Field(..., description="How the conflict was resolved")
    comment: str = ""


class ReviewAction(BaseModel):
    comment: str = ""
