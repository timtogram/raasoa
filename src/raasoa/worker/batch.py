"""Background worker for batch ingestion and maintenance tasks.

Usage:
    uv run python -m raasoa.worker.batch

Tasks:
    - Batch ingest files from a directory or S3 prefix
    - Run tiering sweep (promote/demote documents between tiers)
    - Re-embed documents when embedding model changes

This is a simple async loop — no Celery or Redis needed.
"""

import asyncio
import logging
from pathlib import Path

from sqlalchemy import text

from raasoa.config import settings
from raasoa.db import async_session
from raasoa.ingestion.pipeline import ingest_file
from raasoa.ingestion.tiering import run_tiering_sweep
from raasoa.providers.factory import get_embedding_provider

logger = logging.getLogger(__name__)


async def batch_ingest_directory(
    directory: str,
    tenant_id: str = "00000000-0000-0000-0000-000000000001",
) -> dict:
    """Ingest all supported files from a directory.

    Returns a summary of results.
    """
    import uuid

    supported_extensions = {".txt", ".md", ".pdf", ".docx"}
    dir_path = Path(directory)

    if not dir_path.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    files = [
        f for f in dir_path.iterdir()
        if f.is_file() and f.suffix.lower() in supported_extensions
    ]

    logger.info("Found %d files to ingest in %s", len(files), directory)

    provider = get_embedding_provider()
    tid = uuid.UUID(tenant_id)

    results = {
        "total": len(files),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }

    async with async_session() as session:
        # Ensure tenant and source exist
        from raasoa.api.ingestion import _ensure_default_tenant_and_source

        tid, source_id = await _ensure_default_tenant_and_source(session, tid)
        await session.commit()

    for file_path in files:
        try:
            file_data = file_path.read_bytes()
            if not file_data:
                results["skipped"] += 1
                continue

            max_size = settings.max_file_size_mb * 1024 * 1024
            if len(file_data) > max_size:
                results["skipped"] += 1
                logger.warning("Skipping %s: too large (%d bytes)", file_path.name, len(file_data))
                continue

            async with async_session() as session:
                doc, assessment = await ingest_file(
                    session=session,
                    tenant_id=tid,
                    source_id=source_id,
                    file_data=file_data,
                    filename=file_path.name,
                    embedding_provider=provider,
                )
                await session.refresh(doc)

                logger.info(
                    "Ingested: %s (chunks=%d, quality=%.2f)",
                    doc.title or file_path.name,
                    doc.chunk_count,
                    doc.quality_score or 0.0,
                )
                results["success"] += 1

        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"file": file_path.name, "error": str(e)})
            logger.error("Failed to ingest %s: %s", file_path.name, e)

    logger.info(
        "Batch ingest complete: %d success, %d failed, %d skipped",
        results["success"], results["failed"], results["skipped"],
    )
    return results


async def run_maintenance() -> dict:
    """Run all maintenance tasks.

    - Tiering sweep
    - Clean up orphaned quality findings
    """
    stats: dict = {}

    async with async_session() as session:
        # 1. Tiering sweep
        tiering_stats = await run_tiering_sweep(session)
        stats["tiering"] = tiering_stats

        # 2. Clean up orphaned quality findings (from deleted docs)
        result = await session.execute(
            text(
                "DELETE FROM quality_findings qf "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM documents d WHERE d.id = qf.document_id"
                ")"
            )
        )
        stats["orphaned_findings_cleaned"] = result.rowcount
        await session.commit()

    logger.info("Maintenance complete: %s", stats)
    return stats


async def main() -> None:
    """Run batch operations based on command line arguments."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="RAASOA Background Worker")
    sub = parser.add_subparsers(dest="command")

    ingest_cmd = sub.add_parser("ingest", help="Batch ingest files from a directory")
    ingest_cmd.add_argument("directory", help="Path to directory with files")
    ingest_cmd.add_argument("--tenant", default="00000000-0000-0000-0000-000000000001")

    sub.add_parser("maintenance", help="Run maintenance tasks (tiering, cleanup)")

    sub.add_parser("tiering", help="Run tiering sweep only")

    args = parser.parse_args()

    if args.command == "ingest":
        results = await batch_ingest_directory(args.directory, args.tenant)
        print(f"\nResults: {results}")

    elif args.command == "maintenance":
        stats = await run_maintenance()
        print(f"\nMaintenance results: {stats}")

    elif args.command == "tiering":
        async with async_session() as session:
            stats = await run_tiering_sweep(session)
            print(f"\nTiering results: {stats}")

    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
