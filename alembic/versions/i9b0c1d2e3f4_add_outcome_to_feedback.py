"""Add outcome columns to retrieval_feedback.

Revision ID: i9b0c1d2e3f4
Revises: h8a9b0c1d2e3
Create Date: 2026-04-22
"""

from alembic import op
import sqlalchemy as sa

revision = "i9b0c1d2e3f4"
down_revision = "h8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "retrieval_feedback",
        sa.Column("outcome", sa.Text, nullable=True),
    )
    op.add_column(
        "retrieval_feedback",
        sa.Column("outcome_context", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("retrieval_feedback", "outcome_context")
    op.drop_column("retrieval_feedback", "outcome")
