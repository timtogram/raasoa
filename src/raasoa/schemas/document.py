import uuid
from datetime import datetime

from pydantic import BaseModel


class DocumentSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    source_object_id: str
    doc_type: str | None
    status: str
    chunk_count: int
    version: int
    index_tier: str
    quality_score: float | None
    last_synced_at: datetime | None
    last_embedded_at: datetime | None
    created_at: datetime


class DocumentDetail(DocumentSummary):
    embedding_model: str | None
    review_status: str
    conflict_status: str
    access_count: int


class ChunkDetail(BaseModel):
    id: uuid.UUID
    chunk_index: int
    chunk_text: str
    section_title: str | None
    chunk_type: str
    token_count: int | None
    embedding_model: str | None
    embedded_at: datetime | None


class DocumentWithChunks(DocumentDetail):
    chunks: list[ChunkDetail]


class PaginatedDocuments(BaseModel):
    """Cursor-based paginated response for document listings."""
    items: list[DocumentSummary]
    next_cursor: str | None = None
    has_more: bool = False


class TenantInfo(BaseModel):
    id: uuid.UUID
    name: str


class SourceInfo(BaseModel):
    id: uuid.UUID
    name: str
    source_type: str
