"""Add webhook_idempotency_keys table for F-046 webhook idempotency.

Revision ID: r8e9f0a1b2c3
Revises: q7d8e9f0a1b2
Create Date: 2026-07-06

``WebhookPayload.idempotency_key`` has existed on the webhook ingest
contract since it was first documented, but nothing ever read it —
POST /v1/webhooks/ingest reprocessed a retried request in full every
time, including event shapes with no content-hash-based dedup of their
own (e.g. ``document.deleted``, or a rejected data-contract-violation
response). This table gives the webhook handler an actual place to cache
a terminal, deterministic response per (tenant_id, idempotency_key), so a
retried delivery with the same key short-circuits to the cached response
instead of re-running any processing.

Rows are intentionally short-lived (see the 48h TTL purge added to
``run_retention_cleanup`` in ``src/raasoa/worker/retention.py``) — this
table exists to protect against network-retry duplicate delivery, not to
serve as a permanent audit trail.

Idempotent — see n4a5b6c7d8e9/m3f4a5b6c7d8 for the established
create-table-if-not-exists pattern. Downgrade drops the table (there is
no data-loss concern for this short-lived cache, unlike the
no-op-downgrade tables elsewhere in this lineage).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "r8e9f0a1b2c3"
down_revision = "q7d8e9f0a1b2"
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

    if not _has_table(conn, "webhook_idempotency_keys"):
        op.create_table(
            "webhook_idempotency_keys",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("idempotency_key", sa.Text, nullable=False),
            sa.Column("response_json", JSONB, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id", "idempotency_key",
                name="uq_webhook_idempotency_tenant_key",
            ),
        )
    if not _has_index(conn, "ix_webhook_idempotency_created_at"):
        op.create_index(
            "ix_webhook_idempotency_created_at",
            "webhook_idempotency_keys", ["created_at"],
        )


def downgrade() -> None:
    op.drop_table("webhook_idempotency_keys")
