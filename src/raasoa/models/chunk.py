from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raasoa.config import settings
from raasoa.models.base import Base, UUIDMixin

if TYPE_CHECKING:
    from raasoa.models.document import Document


class Chunk(UUIDMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        Index("idx_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    section_title: Mapped[str | None] = mapped_column(Text)
    chunk_type: Mapped[str] = mapped_column(Text, default="text", server_default="text")
    token_count: Mapped[int | None] = mapped_column(Integer)
    # IMPORTANT: settings.embedding_dimensions is a runtime/env-configurable
    # value, but the underlying Postgres column is NOT dynamically sized —
    # it was created with a fixed width (768) by migration
    # 3a8758ffa2b0_initial_schema_with_foreign_keys.py and has never been
    # ALTERed since. settings.embedding_dimensions must match that actual
    # DB column width for the deployment in use (768 by default, i.e.
    # EMBEDDING_PROVIDER=ollama). Switching EMBEDDING_PROVIDER to one with
    # a different native dimension (e.g. openai -> 1536) WILL NOT resize
    # the column just by changing the env var — it requires a companion
    # migration that explicitly ALTERs chunks.embedding to the new
    # dimension (and re-embeds existing rows). Do not treat this
    # Vector(settings.embedding_dimensions) call as proof the column is
    # dynamically sized; it only reflects what the model expects, not what
    # Postgres actually has.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dimensions)
    )
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default="{}"
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    source_location: Mapped[str | None] = mapped_column(Text)
    tsv: Mapped[str | None] = mapped_column(TSVECTOR)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")  # noqa: F821
