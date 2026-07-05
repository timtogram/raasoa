"""Fix double-quoted tenants.plan server_default and repair corrupted data.

Revision ID: o5b6c7d8e9f0
Revises: n4a5b6c7d8e9
Create Date: 2026-07-05

Root cause: migrations g7b8c9d0e1f2 and j0c1d2e3f4a5 both called
op.add_column("tenants", sa.Column("plan", sa.Text, server_default="'free'"))
— for op.add_column, a plain Python string passed as server_default is
quoted by SQLAlchemy itself when it generates the ALTER TABLE ... ADD
COLUMN ... DEFAULT ... DDL. Passing a string that ALREADY contains
embedded single-quote characters ("'free'") causes SQLAlchemy to quote it
AGAIN, so the DDL ends up with a doubly-quoted literal and the stored
default becomes the 6-character string 'free' (quote characters included
as part of the value), not the intended 4-character word free.

Rows inserted via the ORM (which supplies the Python-side
default="free" on the Tenant model) got the correct 4-character value.
Rows inserted via raw SQL that omitted `plan` fell through to the
corrupted server-side default and got the 6-character value.

This migration:
  1. Repairs existing data — sets plan back to the plain word free
     wherever it currently holds the corrupted quoted value. Guarded by a
     WHERE clause so it is a no-op (idempotent) if run again.
  2. Fixes the default going forward via op.alter_column with
     server_default=sa.text("'free'") — sa.text() marks this as a raw SQL
     expression, telling SQLAlchemy NOT to quote it again, so the DDL
     correctly reads DEFAULT 'free' and the stored default is the
     4-character word.

Idempotent — safe to run twice (matches the has_table/has_column-guarded
style of m3f4a5b6c7d8 / n4a5b6c7d8e9, adapted here to a data-repair +
column-alter instead of table creation).

Downgrade is a no-op — reverting to the corrupted default would
reintroduce a known bug with no compensating benefit, and (per this
repo's established stance in j0c1d2e3f4a5 / m3f4a5b6c7d8 / n4a5b6c7d8e9)
downgrades never re-corrupt or discard data.
"""
from alembic import op
import sqlalchemy as sa

revision = "o5b6c7d8e9f0"
down_revision = "n4a5b6c7d8e9"
branch_labels = None
depends_on = None

_CORRUPTED_PLAN = "'free'"  # the literal 6-character corrupted value


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Repair existing corrupted rows. Idempotent: after the first run,
    #    no row matches the WHERE clause, so this is a no-op on re-run.
    conn.execute(
        sa.text("UPDATE tenants SET plan = 'free' WHERE plan = :corrupted"),
        {"corrupted": _CORRUPTED_PLAN},
    )

    # 2. Fix the default going forward. sa.text(...) marks this as a raw
    #    SQL expression — SQLAlchemy will NOT quote it again, unlike the
    #    plain-string server_default that caused the original bug.
    op.alter_column(
        "tenants",
        "plan",
        server_default=sa.text("'free'"),
    )


def downgrade() -> None:
    # No-op — see module docstring. Never re-introduce the corrupted
    # default or discard the repaired data.
    pass
