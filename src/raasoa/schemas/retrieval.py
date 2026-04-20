from typing import Any

from pydantic import BaseModel, Field


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    principal_id: str | None = Field(
        default=None,
        description="User/group ID for ACL filtering.",
    )
    source_type: str | None = Field(
        default=None,
        description="Pre-filter by source type (e.g. 'sharepoint', 'jira', 'notion').",
    )
    doc_type: str | None = Field(
        default=None,
        description="Pre-filter by document type (e.g. 'pdf', 'policy').",
    )
    metadata_filter: dict[str, str] | None = Field(
        default=None,
        description="Filter by frontmatter metadata. E.g. {'ampel': 'grün', 'executor': 'claude'}",
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
    # Source provenance — link back to original
    document_title: str | None = None
    source_url: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    # Location within document
    page_number: int | None = None
    source_location: str | None = None


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
    outcome: str | None = Field(
        default=None,
        description="Outcome of using this result: success/failure/partial",
    )
    outcome_context: str | None = Field(
        default=None,
        description="What happened when the agent used this result",
    )


class IndexHit(BaseModel):
    """Direct answer from the knowledge index (no embedding needed)."""

    subject: str
    predicate: str
    value: str
    confidence: float
    source_documents: list[str] = []
    valid_from: str | None = None
    valid_until: str | None = None


class RetrieveResponse(BaseModel):
    query: str
    routed_to: str = "rag"
    routing_reason: str = "default_rag"
    # Layer 1: Knowledge Index (fastest, highest confidence)
    index_hits: list[IndexHit] = []
    # Layer 2: Structured SQL answer
    structured: StructuredAnswer | None = None
    # Layer 3: Hybrid search chunks
    results: list[ChunkHit] = []
    # Overall confidence
    confidence: ConfidenceInfo
