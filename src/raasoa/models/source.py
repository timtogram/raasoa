import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raasoa.models.base import Base, UUIDMixin


class Source(UUIDMixin, Base):
    __tablename__ = "sources"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    connection_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="sources")  # noqa: F821
    documents: Mapped[list["Document"]] = relationship(back_populates="source")  # noqa: F821


class SyncCursor(Base):
    __tablename__ = "sync_cursors"

    source_type: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id"), primary_key=True
    )
    delta_token: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sync_status: Mapped[str] = mapped_column(Text, default="idle", server_default="idle")
    error_message: Mapped[str | None] = mapped_column(Text)
    items_synced: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
