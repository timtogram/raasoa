from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Integer, Text, func, text
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
    # server_default uses text("'free'") (a raw SQL literal), NOT the
    # plain string "'free'" — passing an already-quoted Python string as
    # server_default gets quoted AGAIN by SQLAlchemy, corrupting the
    # actual stored default to the 6-character "'free'" (quote
    # characters included). See alembic o5b6c7d8e9f0 for the fix and
    # full root-cause writeup.
    plan: Mapped[str | None] = mapped_column(
        Text, default="free", server_default=text("'free'"),
    )
    max_documents: Mapped[int | None] = mapped_column(Integer, default=100, server_default="100")
    max_queries_per_month: Mapped[int | None] = mapped_column(
        Integer, default=1000, server_default="1000",
    )
    # 10, not 1 -- see alembic s9f0a1b2c3d4. A single-tenant deployment
    # with more than one knowledge source (e.g. Notion + SharePoint) needs
    # to provision at least a handful of named connectors plus the
    # auto-created file-upload pseudo-source.
    max_sources: Mapped[int | None] = mapped_column(Integer, default=10, server_default="10")

    # Admin API gate — see alembic m3f4a5b6c7d8. Default false so existing
    # legacy tenant-wide keys don't silently become admin keys on deploy.
    admin_api_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    sources: Mapped[list[Source]] = relationship(back_populates="tenant")  # noqa: F821
    documents: Mapped[list[Document]] = relationship(back_populates="tenant")  # noqa: F821
