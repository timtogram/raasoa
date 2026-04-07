"""Add temporal validity to claims + source filter index.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-07

Temporal claims: valid_from/valid_until for time-bounded facts.
Source filter index: faster pre-filtered hybrid search.
"""

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Temporal validity on claims
    op.add_column("claims", sa.Column("valid_from", sa.Text, nullable=True))
    op.add_column("claims", sa.Column("valid_until", sa.Text, nullable=True))

    # Index for source-type filtered search
    op.create_index(
        "ix_documents_tenant_source",
        "documents",
        ["tenant_id", "source_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_tenant_source")
    op.drop_column("claims", "valid_until")
    op.drop_column("claims", "valid_from")
