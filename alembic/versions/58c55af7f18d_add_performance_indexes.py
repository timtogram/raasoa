"""add performance indexes

Revision ID: 58c55af7f18d
Revises: 596e6539af2a
Create Date: 2026-04-01 21:01:51.935527
"""
from typing import Sequence, Union

from alembic import op

revision: str = '58c55af7f18d'
down_revision: Union[str, None] = '596e6539af2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Content hash index for duplicate/overlap detection
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_content_hash "
        "ON chunks (content_hash)"
    )

    # HNSW index for vector similarity search (if not already created by ORM)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw "
        "ON chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )

    # Claims indexes for conflict detection
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_claims_tenant_status "
        "ON claims (tenant_id, status) WHERE status = 'active'"
    )

    # Conflict candidates index
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_conflicts_tenant_status "
        "ON conflict_candidates (tenant_id, status)"
    )

    # Review tasks index
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reviews_tenant_status "
        "ON review_tasks (tenant_id, status)"
    )

    # Quality findings by document
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quality_findings_doc "
        "ON quality_findings (document_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunks_content_hash")
    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_claims_tenant_status")
    op.execute("DROP INDEX IF EXISTS idx_conflicts_tenant_status")
    op.execute("DROP INDEX IF EXISTS idx_reviews_tenant_status")
    op.execute("DROP INDEX IF EXISTS idx_quality_findings_doc")
