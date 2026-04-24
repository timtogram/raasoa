"""Add content_snapshot to document_versions for text-level diffs.

Revision ID: k1d2e3f4a5b6
Revises: j0c1d2e3f4a5
Create Date: 2026-04-23
"""

from alembic import op
import sqlalchemy as sa

revision = "k1d2e3f4a5b6"
down_revision = "j0c1d2e3f4a5"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return r.first() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "document_versions", "content_snapshot"):
        op.add_column(
            "document_versions",
            sa.Column("content_snapshot", sa.Text, nullable=True),
        )


def downgrade() -> None:
    op.drop_column("document_versions", "content_snapshot")
