from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raasoa.models.base import Base, UUIDMixin

if TYPE_CHECKING:
    from raasoa.models.document import Document
    from raasoa.models.source import Source


class Tenant(UUIDMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Retention / GDPR
    retention_days: Mapped[int | None] = mapped_column(Integer, default=365, server_default="365")
    hard_delete_enabled: Mapped[bool | None] = mapped_column(
        Boolean, default=False, server_default="false",
    )

    # Plan / quotas
    plan: Mapped[str | None] = mapped_column(Text, default="free", server_default="'free'")
    max_documents: Mapped[int | None] = mapped_column(Integer, default=100, server_default="100")
    max_queries_per_month: Mapped[int | None] = mapped_column(
        Integer, default=1000, server_default="1000",
    )
    max_sources: Mapped[int | None] = mapped_column(Integer, default=1, server_default="1")

    sources: Mapped[list[Source]] = relationship(back_populates="tenant")  # noqa: F821
    documents: Mapped[list[Document]] = relationship(back_populates="tenant")  # noqa: F821
