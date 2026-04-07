"""Add performance indices v2 for multi-tenant scale.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-08

Composite indices for the most common query patterns.
"""

from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Documents: tenant + status (most common filter)
    op.create_index(
        "ix_documents_tenant_status_v2",
        "documents",
        ["tenant_id", "status", "review_status"],
    )

    # Claims: tenant + subject + status (for index building)
    op.create_index(
        "ix_claims_tenant_subject_status",
        "claims",
        ["tenant_id", "status"],
        postgresql_where="status = 'active'",
    )

    # Retrieval logs: by time (for metrics queries)
    op.create_index(
        "ix_retrieval_logs_created",
        "retrieval_logs",
        ["created_at"],
    )

    # Feedback: tenant + chunk (for boost lookup)
    op.create_index(
        "ix_feedback_tenant_chunk",
        "retrieval_feedback",
        ["tenant_id", "chunk_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_tenant_chunk")
    op.drop_index("ix_retrieval_logs_created")
    op.drop_index("ix_claims_tenant_subject_status")
    op.drop_index("ix_documents_tenant_status_v2")
