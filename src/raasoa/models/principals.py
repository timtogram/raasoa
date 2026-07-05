import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from raasoa.models.base import Base, UUIDMixin


class PrincipalGroup(UUIDMixin, Base):
    """A named group principal (e.g. "group:sales") — see alembic
    m3f4a5b6c7d8. Pairs with PrincipalMembership to form an arbitrary
    group-membership graph; see raasoa.security.principal."""

    __tablename__ = "principal_groups"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "principal_id", name="uq_principal_groups_tenant_pid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    # NOTE: unlike tenants.plan's server_default="'free'" (a Python string
    # that ALREADY contains embedded quote characters, which SQLAlchemy
    # then re-quotes — see alembic o5b6c7d8e9f0), this migration's
    # server_default="manual" is a plain unquoted string, which
    # SQLAlchemy quotes exactly once. Verified by direct DDL compilation:
    # this does NOT reproduce the double-quoting bug and needs no fix.
    origin: Mapped[str | None] = mapped_column(Text, server_default="manual")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PrincipalMembership(UUIDMixin, Base):
    """One edge in the group-membership graph: member_principal_id is a
    member of group_principal_id — see alembic m3f4a5b6c7d8."""

    __tablename__ = "principal_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_principal_id", "group_principal_id",
            name="uq_principal_memberships_tenant_member_group",
        ),
        Index(
            "ix_principal_memberships_tenant_member",
            "tenant_id", "member_principal_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    group_principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceAclGrant(UUIDMixin, Base):
    """A grant that applies to ALL current+future documents from a
    source, keyed on source_id — see alembic m3f4a5b6c7d8."""

    __tablename__ = "source_acl_grants"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_id", "principal_id",
            name="uq_source_acl_grants_tenant_source_principal",
        ),
        Index("ix_source_acl_grants_source", "source_id", "principal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    permission: Mapped[str] = mapped_column(Text, nullable=False, server_default="read")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
