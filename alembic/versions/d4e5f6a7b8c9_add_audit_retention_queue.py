"""Add audit_events, retention policies, job queue tables.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Audit log — compliance-grade event tracking
    op.create_table(
        "audit_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.Text, nullable=False),  # API key ID or "system"
        sa.Column("action", sa.Text, nullable=False),  # e.g. "document.ingest"
        sa.Column("resource_type", sa.Text, nullable=False),  # "document", "conflict"
        sa.Column("resource_id", sa.Text),
        sa.Column("details", JSONB, server_default="{}"),
        sa.Column("ip_address", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_audit_tenant_created", "audit_events", ["tenant_id", "created_at"])
    op.create_index("ix_audit_resource", "audit_events", ["resource_type", "resource_id"])

    # Job queue — async task processing
    op.create_table(
        "job_queue",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.Text, nullable=False),  # "ingest", "curate", "cleanup"
        sa.Column("payload", JSONB, server_default="{}"),
        sa.Column("status", sa.Text, server_default="'pending'"),  # pending/running/done/failed
        sa.Column("priority", sa.Integer, server_default="0"),
        sa.Column("attempts", sa.Integer, server_default="0"),
        sa.Column("max_attempts", sa.Integer, server_default="3"),
        sa.Column("error_message", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jobs_pending", "job_queue", ["status", "priority", "created_at"])

    # Retention policy per tenant
    op.add_column("tenants", sa.Column(
        "retention_days", sa.Integer, server_default="365",
    ))
    op.add_column("tenants", sa.Column(
        "hard_delete_enabled", sa.Boolean, server_default="false",
    ))


def downgrade() -> None:
    op.drop_column("tenants", "hard_delete_enabled")
    op.drop_column("tenants", "retention_days")
    op.drop_table("job_queue")
    op.drop_table("audit_events")
