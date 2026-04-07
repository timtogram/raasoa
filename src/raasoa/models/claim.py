import uuid
from datetime import datetime

from sqlalchemy import REAL, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from raasoa.models.base import Base, UUIDMixin


class Claim(UUIDMixin, Base):
    __tablename__ = "claims"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL")
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, default=0.0)
    evidence_span: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        Text, default="active", server_default="active"
    )  # "active", "superseded", "rejected"
    # Temporal validity — when this fact is/was true
    valid_from: Mapped[str | None] = mapped_column(
        Text, default=None
    )  # e.g. "2026-01-01", "Q3 2026", "March 2025"
    valid_until: Mapped[str | None] = mapped_column(
        Text, default=None
    )  # e.g. "2026-12-31", None = still valid
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
