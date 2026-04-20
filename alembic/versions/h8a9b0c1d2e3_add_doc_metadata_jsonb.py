"""Add doc_metadata JSONB column to documents.

Revision ID: h8a9b0c1d2e3
Revises: 19dc365e7974
Create Date: 2026-04-20

Stores structured frontmatter metadata for queryable filtering:
  doc_metadata->>'ampel' = 'grün'
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "h8a9b0c1d2e3"
down_revision = "19dc365e7974"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column(
        "doc_metadata", JSONB, nullable=True,
    ))
    # GIN index for fast JSONB queries
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_doc_metadata "
        "ON documents USING gin(doc_metadata) "
        "WHERE doc_metadata IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_doc_metadata")
    op.drop_column("documents", "doc_metadata")
