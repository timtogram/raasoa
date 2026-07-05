import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from raasoa.models.base import Base, UUIDMixin


class CrmObject(UUIDMixin, Base):
    """Structured CRM records (deals/contacts/companies/tickets) synced
    alongside `documents` for fast typed filtering — see alembic
    n4a5b6c7d8e9. ``owner_principal_id`` is deliberately a plain TEXT
    column, not an FK into acl_entries — see the migration docstring."""

    __tablename__ = "crm_objects"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_id", "object_type", "external_id",
            name="uq_crm_objects_tenant_source_type_external",
        ),
        Index("ix_crm_objects_tenant_type", "tenant_id", "object_type"),
        Index("ix_crm_objects_owner", "tenant_id", "owner_principal_id"),
        Index("ix_crm_objects_properties_gin", "properties", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_principal_id: Mapped[str | None] = mapped_column(Text)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
