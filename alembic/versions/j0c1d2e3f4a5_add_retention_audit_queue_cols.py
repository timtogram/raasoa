"""Add missing columns for retention, audit_events, and plan quotas.

Revision ID: j0c1d2e3f4a5
Revises: i9b0c1d2e3f4
Create Date: 2026-04-22

Adds columns that were in migration d4e5f6a7b8c9 but got lost
in the head merge. Idempotent — only adds what's missing.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "j0c1d2e3f4a5"
down_revision = "i9b0c1d2e3f4"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return r.first() is not None


def _has_table(conn, table: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = :t"
    ), {"t": table})
    return r.first() is not None


def upgrade() -> None:
    conn = op.get_bind()

    # Tenants: retention + plan columns
    if not _has_column(conn, "tenants", "retention_days"):
        op.add_column("tenants", sa.Column(
            "retention_days", sa.Integer, server_default="365",
        ))
    if not _has_column(conn, "tenants", "hard_delete_enabled"):
        op.add_column("tenants", sa.Column(
            "hard_delete_enabled", sa.Boolean, server_default="false",
        ))
    if not _has_column(conn, "tenants", "plan"):
        op.add_column("tenants", sa.Column(
            "plan", sa.Text, server_default="'free'",
        ))
    if not _has_column(conn, "tenants", "max_documents"):
        op.add_column("tenants", sa.Column(
            "max_documents", sa.Integer, server_default="100",
        ))
    if not _has_column(conn, "tenants", "max_queries_per_month"):
        op.add_column("tenants", sa.Column(
            "max_queries_per_month", sa.Integer, server_default="1000",
        ))
    if not _has_column(conn, "tenants", "max_sources"):
        op.add_column("tenants", sa.Column(
            "max_sources", sa.Integer, server_default="1",
        ))

    # Audit events table
    if not _has_table(conn, "audit_events"):
        op.create_table(
            "audit_events",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("actor", sa.Text, nullable=False),
            sa.Column("action", sa.Text, nullable=False),
            sa.Column("resource_type", sa.Text, nullable=False),
            sa.Column("resource_id", sa.Text),
            sa.Column("details", JSONB, server_default="{}"),
            sa.Column("ip_address", sa.Text),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_audit_tenant_created",
            "audit_events",
            ["tenant_id", "created_at"],
        )


def downgrade() -> None:
    # No-op — these columns may be referenced elsewhere
    pass
