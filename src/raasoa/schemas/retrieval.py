import uuid

from pydantic import BaseModel, Field


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    tenant_id: uuid.UUID
    top_k: int = Field(default=5, ge=1, le=50)


class ChunkHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    text: str
    section_title: str | None
    chunk_type: str
    score: float
    semantic_rank: int | None
    lexical_rank: int | None


class ConfidenceInfo(BaseModel):
    retrieval_confidence: float
    source_count: int
    top_score: float
    answerable: bool


class StructuredAnswer(BaseModel):
    """Response from structured (non-RAG) query."""
    answer: str
    data: list[dict]
    query_type: str


class RetrieveResponse(BaseModel):
    query: str
    routed_to: str = "rag"
    routing_reason: str = "default_rag"
    results: list[ChunkHit] = []
    structured: StructuredAnswer | None = None
    confidence: ConfidenceInfo
