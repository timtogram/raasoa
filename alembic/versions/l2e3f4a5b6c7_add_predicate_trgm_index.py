"""Add trigram GIN index on claims.predicate for graph similarity queries.

Revision ID: l2e3f4a5b6c7
Revises: k1d2e3f4a5b6
Create Date: 2026-04-25

The dependency-graph endpoint uses ``similarity(LOWER(predicate), …)``
to match fuzzy LLM-extracted predicates. Without an index that turns
into a sequential scan over claims × claims. A GIN index on
``lower(predicate) gin_trgm_ops`` accelerates the join from O(n²)
to roughly O(n log n) for typical tenant sizes.

Idempotent — checks if the index exists before creating.
"""
from alembic import op
import sqlalchemy as sa

revision = "l2e3f4a5b6c7"
down_revision = "k1d2e3f4a5b6"
branch_labels = None
depends_on = None


def _has_index(conn, name: str) -> bool:
    r = conn.execute(sa.text(
        "SELECT 1 FROM pg_indexes WHERE indexname = :n"
    ), {"n": name})
    return r.first() is not None


def upgrade() -> None:
    conn = op.get_bind()

    # pg_trgm is required (already installed for hybrid search)
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    if not _has_index(conn, "ix_claims_predicate_trgm"):
        op.execute(sa.text(
            "CREATE INDEX ix_claims_predicate_trgm "
            "ON claims USING gin (lower(predicate) gin_trgm_ops)"
        ))

    # Bonus: btree on tenant_id+status for the graph filter that
    # already lives in the same query path.
    if not _has_index(conn, "ix_claims_tenant_status"):
        op.execute(sa.text(
            "CREATE INDEX ix_claims_tenant_status "
            "ON claims (tenant_id, status)"
        ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_claims_predicate_trgm"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_claims_tenant_status"))
