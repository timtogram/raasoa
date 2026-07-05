"""Fix the same double-quoted server_default bug on 3 more columns.

Revision ID: p6c7d8e9f0a1
Revises: o5b6c7d8e9f0
Create Date: 2026-07-05

o5b6c7d8e9f0 fixed tenants.plan's corrupted server_default (SQLAlchemy
re-quotes a Python string that already contains embedded quote
characters, e.g. server_default="'free'", producing a doubly-quoted SQL
literal). A full sweep of every migration using this exact
server_default="'word'" pattern (grep alembic/versions for
'server_default="\'' — note the embedded quote right after the opening
string quote) turned up three more instances, verified by directly
compiling each column's CREATE TABLE DDL against the current migration
file content (not just inspecting this one long-lived dev database,
since a migration file can be edited after it was first applied here
without the already-existing table ever seeing the new DDL — the
authoritative check is what the CURRENT file content produces, since
that's what every fresh deployment / CI run / new clone actually gets):

  - knowledge_index.status (b2c3d4e5f6a7) — confirmed corrupted on THIS
    database too (server_default renders as '''active'''::text).
  - knowledge_syntheses.status (a1b2c3d4e5f6) — same as above.
  - job_queue.status (d4e5f6a7b8c9) — NOT corrupted on this particular
    long-lived dev database (it was evidently created before this file
    was last edited), but compiling the CURRENT file's
    sa.Column("status", sa.Text, server_default="'pending'") in
    isolation reproduces the identical '''pending'''::text corruption —
    so a fresh `alembic upgrade head` (a new deployment, CI, or anyone
    cloning this repo today) WOULD get the corrupted default even though
    this dev database currently doesn't show it.

None of the three tables have any actually-corrupted existing rows in
this database (grep the codebase: every INSERT into these tables sets
status explicitly rather than relying on the column default — see
raasoa.retrieval.knowledge_index._insert_index_entries and
raasoa.worker.queue.enqueue), so the data-repair UPDATEs below are
defensive (idempotent, no-op if nothing matches) rather than fixing
observed corruption, unlike o5b6c7d8e9f0 which repaired real bad rows.

Idempotent — safe to run twice. Downgrade is a no-op, matching this
repo's established no-data-loss-on-downgrade convention.
"""
from alembic import op
import sqlalchemy as sa

revision = "p6c7d8e9f0a1"
down_revision = "o5b6c7d8e9f0"
branch_labels = None
depends_on = None

# (table, column, correct_value, corrupted_value)
_FIXES = [
    ("knowledge_index", "status", "active", "'active'"),
    ("knowledge_syntheses", "status", "active", "'active'"),
    ("job_queue", "status", "pending", "'pending'"),
]


def upgrade() -> None:
    conn = op.get_bind()

    for table, column, correct, corrupted in _FIXES:
        # 1. Defensive data repair — no-op if no row is actually corrupted.
        conn.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :correct "
                f"WHERE {column} = :corrupted"
            ),
            {"correct": correct, "corrupted": corrupted},
        )
        # 2. Fix the default going forward. sa.text(...) marks this as a
        #    raw SQL expression — SQLAlchemy will NOT quote it again,
        #    unlike the plain-string server_default that caused the bug.
        op.alter_column(
            table,
            column,
            server_default=sa.text(f"'{correct}'"),
        )


def downgrade() -> None:
    # No-op — see module docstring. Never re-introduce the corrupted
    # default or discard the (defensive) data repair.
    pass
