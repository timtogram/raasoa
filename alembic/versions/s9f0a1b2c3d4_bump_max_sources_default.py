"""Bump tenants.max_sources default from 1 to 10.

Revision ID: s9f0a1b2c3d4
Revises: r8e9f0a1b2c3
Create Date: 2026-07-06

max_sources=1 blocks provisioning a second connector (e.g. Notion +
SharePoint, the minimum needed for a real single-tenant deployment with
more than one knowledge source) via the documented POST /v1/sources
path -- the very first call to add a second source returns
"Source limit reached (1/1)." A self-hosted single-tenant deployment
realistically needs several named connectors plus the auto-created
file-upload pseudo-source, so 1 was never a sensible default outside of
a pure single-source demo.

Also updates existing tenant rows still at the old default (1) --
without this, a tenant already created under the old default keeps the
old value even after this migration changes the column default for
future inserts.

Idempotent — safe to run twice.
"""
from alembic import op
import sqlalchemy as sa

revision = "s9f0a1b2c3d4"
down_revision = "r8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE tenants SET max_sources = 10 WHERE max_sources = 1"))
    op.alter_column("tenants", "max_sources", server_default="10")


def downgrade() -> None:
    op.alter_column("tenants", "max_sources", server_default="1")
