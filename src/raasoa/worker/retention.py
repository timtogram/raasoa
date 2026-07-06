"""Data retention and GDPR hard-delete.

Permanently removes soft-deleted documents and their associated data
(chunks, claims, embeddings, quality findings) after the configured
retention period.

Usage:
    uv run python -m raasoa.worker.retention
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from raasoa.db import async_session

logger = logging.getLogger(__name__)


async def run_retention_cleanup() -> dict[str, int]:
    """Hard-delete expired soft-deleted records.

    Removes documents with status='deleted' that are older than
    the tenant's retention_days setting.
    """
    stats = {
        "documents_purged": 0,
        "chunks_purged": 0,
        "claims_purged": 0,
        "findings_purged": 0,
        "feedback_purged": 0,
        "acl_entries_purged": 0,
        "crm_objects_purged": 0,
        "idempotency_keys_purged": 0,
    }

    async with async_session() as session:
        # Webhook idempotency keys are short-lived by design (they exist
        # to protect against network-retry duplicate delivery, not to
        # serve as a permanent audit trail) — purge anything older than
        # 48h regardless of whether any documents are expired this cycle.
        idem_result = await session.execute(
            text(
                "DELETE FROM webhook_idempotency_keys "
                "WHERE created_at < now() - interval '48 hours'"
            )
        )
        stats["idempotency_keys_purged"] = idem_result.rowcount or 0  # type: ignore[attr-defined]
        await session.commit()

        # Find expired soft-deleted documents
        result = await session.execute(
            text(
                "SELECT d.id FROM documents d "
                "JOIN tenants t ON d.tenant_id = t.id "
                "WHERE d.status = 'deleted' "
                "AND t.hard_delete_enabled = true "
                "AND d.created_at < now() - "
                "  (COALESCE(t.retention_days, 365) || ' days')::interval"
            )
        )
        doc_ids = [r.id for r in result.fetchall()]

        if not doc_ids:
            logger.info("No expired documents to purge")
            return stats

        for doc_id in doc_ids:
            # Delete in order: feedback, findings, acl_entries, crm_objects,
            # claims, chunks, versions, doc.
            #
            # acl_entries and crm_objects have NO foreign key to documents
            # (unlike chunks/claims, which FK-cascade), so a tenant
            # retention-driven hard delete must explicitly purge them here
            # too, or a document's ACL grants and CRM object row are
            # orphaned permanently even after the document row itself is
            # gone.
            for table, col in [
                ("retrieval_feedback", "document_id"),
                ("quality_findings", "document_id"),
                ("acl_entries", "document_id"),
                ("crm_objects", "document_id"),
                ("claims", "document_id"),
                ("chunks", "document_id"),
                ("document_versions", "document_id"),
            ]:
                r = await session.execute(
                    text(f"DELETE FROM {table} WHERE {col} = :did"),
                    {"did": doc_id},
                )
                count = r.rowcount or 0  # type: ignore[attr-defined]
                if table == "chunks":
                    stats["chunks_purged"] += count
                elif table == "claims":
                    stats["claims_purged"] += count
                elif table == "quality_findings":
                    stats["findings_purged"] += count
                elif table == "retrieval_feedback":
                    stats["feedback_purged"] += count
                elif table == "acl_entries":
                    stats["acl_entries_purged"] += count
                elif table == "crm_objects":
                    stats["crm_objects_purged"] += count

            # Delete the document itself
            await session.execute(
                text("DELETE FROM documents WHERE id = :did"),
                {"did": doc_id},
            )
            stats["documents_purged"] += 1

        await session.commit()
        logger.info("Retention cleanup: %s", stats)

    return stats


async def main() -> None:

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    stats = await run_retention_cleanup()
    print(f"Retention cleanup: {stats}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
