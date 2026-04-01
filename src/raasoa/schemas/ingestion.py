import uuid

from pydantic import BaseModel


class QualityFindingSummary(BaseModel):
    finding_type: str
    severity: str
    details: dict | None = None


class IngestResponse(BaseModel):
    document_id: uuid.UUID
    title: str | None
    status: str
    chunk_count: int
    version: int
    embedding_model: str | None
    quality_score: float | None = None
    review_status: str = "auto_published"
    conflict_status: str = "none"
    quality_findings: list[QualityFindingSummary] = []
    message: str
