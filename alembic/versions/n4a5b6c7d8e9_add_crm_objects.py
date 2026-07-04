"""Add crm_objects table for the structured CRM query path.

Revision ID: n4a5b6c7d8e9
Revises: m3f4a5b6c7d8
Create Date: 2026-07-04

Task #15 ("strukturierter CRM-Query-Pfad"): connectors like HubSpot sync
each record into `documents` (for RAG/hybrid search) AND into this table
(for fast, structured, typed filtering — "deals over $10k in stage
closedwon" is a bad fit for vector search but a one-line WHERE clause
here).

`owner_principal_id` is a plain TEXT column, NOT a foreign key into
acl_entries — CRM records are per-owner sensitive by nature (see the
HubSpot connector's owner-based ACL grant on the paired `documents` row),
and duplicating that per-owner check as a column here lets
POST /v1/crm/query enforce it with a single predicate instead of a
correlated join into acl_entries for every row of a potentially large
result set.

Idempotent — see m3f4a5b6c7d8 for the established pattern. Downgrade is a
no-op (data-loss prevention stance, consistent with every migration in
this file's lineage).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "n4a5b6c7d8e9"
down_revision = "m3f4a5b6c7d8"
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
    ), {"t": table})
    return r.first() is not None


def _has_index(conn, name: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM pg_indexes WHERE indexname = :n"
    ), {"n": name})
    return r.first() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "crm_objects"):
        op.create_table(
            "crm_objects",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("source_id", UUID(as_uuid=True), nullable=False),
            sa.Column("document_id", UUID(as_uuid=True), nullable=True),
            sa.Column("object_type", sa.Text, nullable=False),
            sa.Column("external_id", sa.Text, nullable=False),
            sa.Column("owner_principal_id", sa.Text, nullable=True),
            sa.Column("properties", JSONB, nullable=False,
                      server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "source_id", "object_type", "external_id",
                name="uq_crm_objects_tenant_source_type_external",
            ),
        )
    if not _has_index(conn, "ix_crm_objects_tenant_type"):
        op.create_index(
            "ix_crm_objects_tenant_type", "crm_objects", ["tenant_id", "object_type"],
        )
    if not _has_index(conn, "ix_crm_objects_owner"):
        op.create_index(
            "ix_crm_objects_owner", "crm_objects", ["tenant_id", "owner_principal_id"],
        )
    if not _has_index(conn, "ix_crm_objects_properties_gin"):
        op.create_index(
            "ix_crm_objects_properties_gin", "crm_objects", ["properties"],
            postgresql_using="gin",
        )


def downgrade() -> None:
    # No-op — see module docstring.
    pass
