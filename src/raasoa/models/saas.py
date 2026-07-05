import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from raasoa.models.base import Base, UUIDMixin


class ApiKey(UUIDMixin, Base):
    """DB-managed API keys — see alembic g7b8c9d0e1f2 (create) and
    m3f4a5b6c7d8 (principal_id / clearance / is_admin columns for
    personal, ACL-aware keys — see raasoa.security.principal).

    key_hash's uniqueness comes from the inline ``unique=True`` in
    g7b8c9d0e1f2's op.create_table, which Postgres auto-names
    ``api_keys_key_hash_key`` — kept as ``unique=True`` here (not a named
    UniqueConstraint) to match that exact auto-generated name.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_hash", "key_hash"),
        Index("ix_api_keys_tenant", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[Any] | None] = mapped_column(JSONB, server_default='["all"]')
    is_active: Mapped[bool | None] = mapped_column(Boolean, server_default="true")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # NULL principal_id = legacy/tenant-wide key. See raasoa.security.principal.
    principal_id: Mapped[str | None] = mapped_column(Text)
    clearance: Mapped[str] = mapped_column(Text, nullable=False, server_default="public")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class UsageEvent(UUIDMixin, Base):
    """Append-only usage metering log — see alembic g7b8c9d0e1f2."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_tenant_type_time", "tenant_id", "event_type", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer, server_default="1")
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, server_default="{}"
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
