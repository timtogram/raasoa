"""Add usage metering, tenant quotas, and API key management.

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-04-12

Foundation for SaaS: track usage per tenant, enforce limits,
and manage API keys without editing .env files.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "g7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # API keys — managed in DB, not .env
    op.create_table(
        "api_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("key_hash", sa.Text, nullable=False, unique=True),
        sa.Column("key_prefix", sa.Text, nullable=False),  # "sk-abc..." for display
        sa.Column("name", sa.Text, nullable=False),  # "Production Key"
        sa.Column("scopes", JSONB, server_default='["all"]'),
        sa.Column("is_active", sa.Boolean, server_default="true"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_api_keys_hash", "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_tenant", "api_keys", ["tenant_id"])

    # Usage metering — append-only event log
    op.create_table(
        "usage_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        # "ingest", "retrieve", "llm_call", "embedding_call"
        sa.Column("quantity", sa.Integer, server_default="1"),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_usage_tenant_type_time",
        "usage_events",
        ["tenant_id", "event_type", "created_at"],
    )

    # Tenant quotas
    op.add_column("tenants", sa.Column("plan", sa.Text, server_default="'free'"))
    op.add_column("tenants", sa.Column("max_documents", sa.Integer, server_default="100"))
    op.add_column("tenants", sa.Column("max_queries_per_month", sa.Integer, server_default="1000"))
    op.add_column("tenants", sa.Column("max_sources", sa.Integer, server_default="1"))


def downgrade() -> None:
    op.drop_column("tenants", "max_sources")
    op.drop_column("tenants", "max_queries_per_month")
    op.drop_column("tenants", "max_documents")
    op.drop_column("tenants", "plan")
    op.drop_table("usage_events")
    op.drop_table("api_keys")
