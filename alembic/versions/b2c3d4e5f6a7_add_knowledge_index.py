"""Add knowledge_index table for fast factual lookups.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-07

Materialized lookup index built from claims. Enables sub-5ms
answers for factual queries without embedding.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_index",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        # Normalized entity
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("subject_normalized", sa.Text, nullable=False),
        # Normalized relationship
        sa.Column("predicate", sa.Text, nullable=False),
        sa.Column("predicate_normalized", sa.Text, nullable=False),
        # The answer
        sa.Column("value", sa.Text, nullable=False),
        # Provenance
        sa.Column("source_claim_ids", JSONB, server_default="[]"),
        sa.Column("source_document_ids", JSONB, server_default="[]"),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("claim_count", sa.Integer, server_default="1"),
        # Status
        sa.Column("status", sa.Text, server_default="'active'"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    # Fast lookup by normalized subject + predicate
    op.create_index(
        "ix_knowledge_index_lookup",
        "knowledge_index",
        ["tenant_id", "subject_normalized", "predicate_normalized"],
    )
    # Text search on predicates for fuzzy matching
    op.create_index(
        "ix_knowledge_index_predicate",
        "knowledge_index",
        ["tenant_id", "predicate_normalized"],
    )


def downgrade() -> None:
    op.drop_table("knowledge_index")
