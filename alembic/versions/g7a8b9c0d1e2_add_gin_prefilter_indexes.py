"""Add GIN indexes for pre-filtering before vector scan.

Revision ID: g7a8b9c0d1e2
Revises: f6a7b8c9d0e1
Create Date: 2026-04-14

EdgeQuake showed SQL pre-filtering reduces vector scans by ~90%.
These indexes enable fast WHERE clauses before the expensive
vector distance computation.
"""

from alembic import op

revision = "g7a8b9c0d1e2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trigram index on chunk text for fast LIKE searches
    op.execute(
        "CREATE EXTENSION IF NOT EXISTS pg_trgm"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_text_trgm "
        "ON chunks USING gin(chunk_text gin_trgm_ops)"
    )

    # B-tree on section_title for pre-filtering
    op.create_index(
        "ix_chunks_section_title",
        "chunks",
        ["section_title"],
        postgresql_where="section_title IS NOT NULL",
    )

    # Trigram on knowledge_index predicates (fast LIKE lookup)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ki_predicate_trgm "
        "ON knowledge_index USING gin(predicate_normalized gin_trgm_ops)"
    )

    # Composite: tenant + doc status (covers 90% of WHERE clauses)
    op.create_index(
        "ix_docs_tenant_indexed",
        "documents",
        ["tenant_id"],
        postgresql_where=(
            "status = 'indexed' AND "
            "review_status NOT IN "
            "('quarantined', 'rejected', 'superseded')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_docs_tenant_indexed")
    op.execute("DROP INDEX IF EXISTS ix_ki_predicate_trgm")
    op.drop_index("ix_chunks_section_title")
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_trgm")
