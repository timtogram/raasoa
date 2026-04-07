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
    }

    async with async_session() as session:
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
            # Delete in order: feedback, findings, claims, chunks, versions, doc
            for table, col in [
                ("retrieval_feedback", "document_id"),
                ("quality_findings", "document_id"),
                ("claims", "document_id"),
                ("chunks", "document_id"),
                ("document_versions", "document_id"),
            ]:
                r = await session.execute(
                    text(f"DELETE FROM {table} WHERE {col} = :did"),
                    {"did": doc_id},
                )
                count = r.rowcount or 0  # type: ignore[attr-defined]
                if "chunks" in table:
                    stats["chunks_purged"] += count
                elif "claims" in table:
                    stats["claims_purged"] += count
                elif "findings" in table:
                    stats["findings_purged"] += count
                elif "feedback" in table:
                    stats["feedback_purged"] += count

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
