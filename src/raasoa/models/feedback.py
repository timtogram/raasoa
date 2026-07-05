import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from raasoa.models.base import Base, UUIDMixin


class RetrievalFeedback(UUIDMixin, Base):
    """Cumulative relevance signals — see alembic a1b2c3d4e5f6 (create,
    plus ix_retrieval_feedback_tenant_chunk) and i9b0c1d2e3f4
    (outcome/outcome_context columns) and e5f6a7b8c9d0
    (ix_feedback_tenant_chunk, a second near-duplicate index)."""

    __tablename__ = "retrieval_feedback"
    __table_args__ = (
        Index("ix_retrieval_feedback_tenant_chunk", "tenant_id", "chunk_id"),
        Index("ix_feedback_tenant_chunk", "tenant_id", "chunk_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text)
    outcome_context: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KnowledgeSynthesis(UUIDMixin, Base):
    """LLM-compiled topic summaries — see alembic a1b2c3d4e5f6."""

    __tablename__ = "knowledge_syntheses"
    __table_args__ = (
        Index("ix_knowledge_syntheses_tenant_topic", "tenant_id", "topic"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_document_ids: Mapped[list[Any] | None] = mapped_column(JSONB, server_default="[]")
    source_claim_ids: Mapped[list[Any] | None] = mapped_column(JSONB, server_default="[]")
    claim_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KnowledgeIndexEntry(UUIDMixin, Base):
    """Materialized entity-attribute-value lookup built from claims — see
    alembic b2c3d4e5f6a7. Maps to table ``knowledge_index``; the model is
    named ``KnowledgeIndexEntry`` (not ``KnowledgeIndex``) purely to avoid
    a name clash with the ``raasoa.retrieval.knowledge_index`` module."""

    __tablename__ = "knowledge_index"
    __table_args__ = (
        Index(
            "ix_knowledge_index_lookup",
            "tenant_id", "subject_normalized", "predicate_normalized",
        ),
        Index("ix_knowledge_index_predicate", "tenant_id", "predicate_normalized"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    subject_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    predicate_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_claim_ids: Mapped[list[Any] | None] = mapped_column(JSONB, server_default="[]")
    source_document_ids: Mapped[list[Any] | None] = mapped_column(JSONB, server_default="[]")
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    claim_count: Mapped[int | None] = mapped_column(Integer, server_default="1")
    status: Mapped[str | None] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
