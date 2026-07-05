"""Add the missing id server_default on 7 tables to match UUIDMixin.

Revision ID: q7d8e9f0a1b2
Revises: p6c7d8e9f0a1
Create Date: 2026-07-05

Discovered during the T-25 schema-parity verification (independent of
p6c7d8e9f0a1's double-quoting fix): of the 11 tables added for the new ORM
models, 4 (crm_objects, principal_groups, principal_memberships,
source_acl_grants — all in n4a5b6c7d8e9/m3f4a5b6c7d8) already declare
``sa.Column("id", ..., server_default=sa.text("gen_random_uuid()"))``, but
the other 7 (retrieval_feedback, knowledge_syntheses, knowledge_index,
audit_events, job_queue, api_keys, usage_events) never got one — yet every
one of the 11 models inherits ``UUIDMixin``, which declares that same
server_default unconditionally. So 7 models currently claim a DB-level
default their actual table doesn't have.

This has NOT caused any live bug: every application INSERT into these 7
tables (retrieval/feedback.py, quality/synthesis.py,
retrieval/knowledge_index.py, middleware/audit.py, worker/queue.py,
api/keys.py + api/admin.py + api/tenants.py + dashboard/routes.py,
middleware/metering.py — verified by reading every call site) already
supplies ``id`` explicitly via Python's ``uuid.uuid4()``, so the missing
server-side default was never actually exercised. This migration closes
the drift for consistency and defense-in-depth (e.g. a future raw-SQL
INSERT, or a manual psql insert, that omits ``id``), matching the other 4
tables and the ORM models as they already stand today.

Idempotent — `IF NOT EXISTS`-style guard isn't needed since
`alter_column` with a `server_default` is itself idempotent (re-running it
just re-sets the same default). Downgrade is a no-op, matching this
repo's established no-data-loss-on-downgrade convention.
"""
from alembic import op
import sqlalchemy as sa

revision = "q7d8e9f0a1b2"
down_revision = "p6c7d8e9f0a1"
branch_labels = None
depends_on = None

_TABLES = [
    "retrieval_feedback",
    "knowledge_syntheses",
    "knowledge_index",
    "audit_events",
    "job_queue",
    "api_keys",
    "usage_events",
]


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "id",
            server_default=sa.text("gen_random_uuid()"),
        )


def downgrade() -> None:
    # No-op — see module docstring.
    pass
