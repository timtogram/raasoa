from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Text, func
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

    sources: Mapped[list[Source]] = relationship(back_populates="tenant")  # noqa: F821
    documents: Mapped[list[Document]] = relationship(back_populates="tenant")  # noqa: F821
