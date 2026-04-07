"""Add retrieval_feedback and knowledge_synthesis tables.

Revision ID: a1b2c3d4e5f6
Revises: 58c55af7f18d
Create Date: 2026-04-07

Retrieval feedback: cumulative learning from search result ratings.
Knowledge synthesis: LLM-compiled topic summaries (Karpathy-inspired).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "a1b2c3d4e5f6"
down_revision = "58c55af7f18d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retrieval feedback — cumulative relevance signals
    op.create_table(
        "retrieval_feedback",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("query_text", sa.Text, nullable=False),
        sa.Column("chunk_id", UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rating", sa.Float, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_retrieval_feedback_tenant_chunk",
        "retrieval_feedback",
        ["tenant_id", "chunk_id"],
    )

    # Knowledge synthesis — LLM-compiled topic summaries
    op.create_table(
        "knowledge_syntheses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("source_document_ids", JSONB, server_default="[]"),
        sa.Column("source_claim_ids", JSONB, server_default="[]"),
        sa.Column("claim_count", sa.Integer, server_default="0"),
        sa.Column("confidence", sa.Float),
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
    op.create_index(
        "ix_knowledge_syntheses_tenant_topic",
        "knowledge_syntheses",
        ["tenant_id", "topic"],
    )


def downgrade() -> None:
    op.drop_table("knowledge_syntheses")
    op.drop_table("retrieval_feedback")
