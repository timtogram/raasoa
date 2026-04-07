from typing import Any

from pydantic import BaseModel, Field


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    principal_id: str | None = Field(
        default=None,
        description="User/group ID for ACL filtering. "
        "Only documents accessible to this principal are returned.",
    )


class ChunkHit(BaseModel):
    chunk_id: str
    document_id: str
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
    data: list[dict[str, Any]]
    query_type: str


class FeedbackRequest(BaseModel):
    """Feedback on a retrieval result — makes future searches smarter."""

    query: str = Field(..., min_length=1)
    chunk_id: str = Field(..., description="ID of the chunk being rated")
    document_id: str = Field(..., description="ID of the parent document")
    rating: float = Field(
        ..., ge=-1.0, le=1.0,
        description="Rating: -1.0 (unhelpful) to 1.0 (very helpful)",
    )


class RetrieveResponse(BaseModel):
    query: str
    routed_to: str = "rag"
    routing_reason: str = "default_rag"
    results: list[ChunkHit] = []
    structured: StructuredAnswer | None = None
    confidence: ConfidenceInfo
