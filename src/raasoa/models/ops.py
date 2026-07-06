import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from raasoa.models.base import Base, UUIDMixin


class AuditEvent(UUIDMixin, Base):
    """Compliance-grade audit log — see alembic d4e5f6a7b8c9 (create;
    re-created idempotently by j0c1d2e3f4a5 in case it was lost in a merge,
    but j0c1d2e3f4a5 only re-adds ix_audit_tenant_created, not
    ix_audit_resource)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, server_default="{}")
    ip_address: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class QueuedJob(UUIDMixin, Base):
    """Simple Postgres-backed job queue (SELECT FOR UPDATE SKIP LOCKED) —
    see alembic d4e5f6a7b8c9. Maps to table ``job_queue``; named
    ``QueuedJob`` (not ``Job``) to avoid a generic/ambiguous class name."""

    __tablename__ = "job_queue"
    __table_args__ = (
        Index("ix_jobs_pending", "status", "priority", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str | None] = mapped_column(Text, server_default=text("'pending'"))
    priority: Mapped[int | None] = mapped_column(Integer, server_default="0")
    attempts: Mapped[int | None] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int | None] = mapped_column(Integer, server_default="3")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookIdempotencyKey(UUIDMixin, Base):
    """Cached terminal response per (tenant_id, idempotency_key) so a
    retried webhook delivery short-circuits instead of reprocessing — see
    alembic r8e9f0a1b2c3. Rows are short-lived by design (purged after 48h
    by ``raasoa.worker.retention.run_retention_cleanup``); this is a
    network-retry dedup cache, not a permanent audit trail."""

    __tablename__ = "webhook_idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_webhook_idempotency_tenant_key",
        ),
        Index("ix_webhook_idempotency_created_at", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
