"""Add page_number and source_location to chunks.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-08

Tracks where in the original document a chunk came from:
- page_number: PDF page, PPTX slide number
- source_location: "Page 5", "Slide 3", "Sheet: Revenue"
"""

from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("page_number", sa.Integer, nullable=True))
    op.add_column("chunks", sa.Column("source_location", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "source_location")
    op.drop_column("chunks", "page_number")
