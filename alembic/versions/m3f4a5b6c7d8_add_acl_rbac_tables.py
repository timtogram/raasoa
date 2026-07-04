"""Add ACL/RBAC principal tables, source visibility, per-key identity.

Revision ID: m3f4a5b6c7d8
Revises: l2e3f4a5b6c7
Create Date: 2026-07-04

Adds the schema for principal-based access control beyond per-document
acl_entries:

- principal_groups / principal_memberships: an arbitrary group-membership
  graph, e.g. "user:jane" is a member of "group:sales". Lets an admin grant
  access to a group rather than hand-writing per-document ACL rows for
  every individual.
- source_acl_grants: a grant that applies to ALL current+future documents
  from a source, keyed on source_id (UUID FK) — not a per-document row, so
  granting "the whole HubSpot source" to a group needs one row, and new
  documents synced later automatically inherit it.
- sources.default_visibility: 'inherit' (today's default-open-if-no-ACL
  behavior, unchanged) or 'restricted' (deny by default unless an
  acl_entries row or source_acl_grants row matches the caller).
- api_keys.principal_id / clearance / is_admin: an API key can now carry a
  personal identity (e.g. "user:jane") instead of only mapping to a
  tenant. NULL principal_id means "legacy/tenant-wide key" and must NOT be
  treated as a normal principal string — see raasoa.security.principal.
- tenants.admin_api_enabled: the new /v1/admin/* endpoints are gated on
  this (default false) so existing tenants' legacy keys don't silently
  become admin keys the moment this ships.

Idempotent — every DDL statement checks information_schema first, so this
is safe to re-run (matches the style of j0c1d2e3f4a5 / l2e3f4a5b6c7).
Downgrade is an explicit no-op: dropping populated principal_memberships /
source_acl_grants rows on rollback would be an unrecoverable data-loss
footgun (same stance as j0c1d2e3f4a5).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "m3f4a5b6c7d8"
down_revision = "l2e3f4a5b6c7"
branch_labels = None
depends_on = None


def _has_table(conn, table: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
    ), {"t": table})
    return r.first() is not None


def _has_column(conn, table: str, column: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return r.first() is not None


def _has_index(conn, name: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM pg_indexes WHERE indexname = :n"
    ), {"n": name})
    return r.first() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "principal_groups"):
        op.create_table(
            "principal_groups",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("principal_id", sa.Text, nullable=False),
            sa.Column("display_name", sa.Text),
            # NOTE: server_default must be the bare value ("manual"), not a
            # pre-quoted SQL literal ("'manual'") — SQLAlchemy quotes plain
            # strings for TEXT columns itself; passing an already-quoted
            # string double-quotes it, so the stored default becomes the
            # literal 8-character string 'manual' (with the quote
            # characters included), not the intended word. This exact
            # mistake pre-exists in j0c1d2e3f4a5's tenants.plan default —
            # flagged separately, not fixed here.
            sa.Column("origin", sa.Text, server_default="manual"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "principal_id", name="uq_principal_groups_tenant_pid",
            ),
        )

    if not _has_table(conn, "principal_memberships"):
        op.create_table(
            "principal_memberships",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("member_principal_id", sa.Text, nullable=False),
            sa.Column("group_principal_id", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "member_principal_id", "group_principal_id",
                name="uq_principal_memberships_tenant_member_group",
            ),
        )
    if not _has_index(conn, "ix_principal_memberships_tenant_member"):
        op.create_index(
            "ix_principal_memberships_tenant_member",
            "principal_memberships",
            ["tenant_id", "member_principal_id"],
        )

    if not _has_table(conn, "source_acl_grants"):
        op.create_table(
            "source_acl_grants",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("source_id", UUID(as_uuid=True), nullable=False),
            sa.Column("principal_id", sa.Text, nullable=False),
            sa.Column("permission", sa.Text, nullable=False, server_default="read"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "source_id", "principal_id",
                name="uq_source_acl_grants_tenant_source_principal",
            ),
        )
    if not _has_index(conn, "ix_source_acl_grants_source"):
        op.create_index(
            "ix_source_acl_grants_source",
            "source_acl_grants",
            ["source_id", "principal_id"],
        )

    # api_keys: personal identity + clearance + admin-API flag.
    # NULL principal_id = legacy/tenant-wide key (existing behavior).
    if not _has_column(conn, "api_keys", "principal_id"):
        op.add_column("api_keys", sa.Column("principal_id", sa.Text, nullable=True))
    if not _has_column(conn, "api_keys", "clearance"):
        op.add_column(
            "api_keys",
            sa.Column("clearance", sa.Text, nullable=False, server_default="public"),
        )
    if not _has_column(conn, "api_keys", "is_admin"):
        op.add_column(
            "api_keys",
            sa.Column("is_admin", sa.Boolean, nullable=False, server_default="false"),
        )

    # sources: per-source default visibility.
    if not _has_column(conn, "sources", "default_visibility"):
        op.add_column(
            "sources",
            sa.Column(
                "default_visibility", sa.Text, nullable=False, server_default="inherit",
            ),
        )

    # tenants: explicit opt-in gate for the new /v1/admin/* endpoints, so
    # existing legacy tenant-wide keys (which resolve as admin for
    # backward compatibility — see raasoa.security.principal) don't
    # silently gain admin-API access the moment this ships.
    if not _has_column(conn, "tenants", "admin_api_enabled"):
        op.add_column(
            "tenants",
            sa.Column(
                "admin_api_enabled", sa.Boolean, nullable=False, server_default="false",
            ),
        )


def downgrade() -> None:
    # No-op — see module docstring. Dropping principal_memberships/
    # source_acl_grants rows on rollback is an unrecoverable data-loss
    # footgun; downgrades in this project are best-effort (see
    # j0c1d2e3f4a5's identical stance).
    pass
