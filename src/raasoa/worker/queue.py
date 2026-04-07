"""Simple job queue backed by PostgreSQL.

No Celery/Redis needed — uses the existing DB with SELECT FOR UPDATE SKIP LOCKED
for distributed job processing.

Jobs are created by API endpoints and processed by the worker loop.
Multiple workers can run concurrently without conflicts.

Usage:
    # Run worker (processes jobs continuously)
    uv run python -m raasoa.worker.queue

    # Enqueue a job from API
    await enqueue(session, tenant_id, "curate", {})
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import async_session

logger = logging.getLogger(__name__)


async def enqueue(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    job_type: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
) -> uuid.UUID:
    """Add a job to the queue."""
    import json

    job_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO job_queue "
            "(id, tenant_id, job_type, payload, priority) "
            "VALUES (:id, :tid, :jtype, CAST(:payload AS jsonb), :prio)"
        ),
        {
            "id": job_id,
            "tid": tenant_id,
            "jtype": job_type,
            "payload": json.dumps(payload or {}),
            "prio": priority,
        },
    )
    return job_id


async def process_one() -> bool:
    """Claim and process one job. Returns True if a job was processed."""
    async with async_session() as session:
        # Claim a job with SKIP LOCKED (safe for concurrent workers)
        result = await session.execute(
            text(
                "UPDATE job_queue SET status = 'running', "
                "started_at = now(), attempts = attempts + 1 "
                "WHERE id = ("
                "  SELECT id FROM job_queue "
                "  WHERE status = 'pending' "
                "  AND attempts < max_attempts "
                "  ORDER BY priority DESC, created_at ASC "
                "  LIMIT 1 "
                "  FOR UPDATE SKIP LOCKED"
                ") RETURNING id, tenant_id, job_type, payload"
            )
        )
        job = result.first()
        if not job:
            return False

        logger.info("Processing job %s: %s", job.id, job.job_type)

        try:
            await _execute_job(session, job)

            await session.execute(
                text(
                    "UPDATE job_queue SET status = 'done', "
                    "completed_at = now() WHERE id = :jid"
                ),
                {"jid": job.id},
            )
            await session.commit()
            logger.info("Job %s completed", job.id)
            return True

        except Exception as e:
            logger.exception("Job %s failed: %s", job.id, e)
            await session.rollback()

            async with async_session() as err_session:
                await err_session.execute(
                    text(
                        "UPDATE job_queue SET status = 'failed', "
                        "error_message = :err WHERE id = :jid"
                    ),
                    {"jid": job.id, "err": str(e)[:500]},
                )
                await err_session.commit()
            return True


async def _execute_job(session: AsyncSession, job: Any) -> None:
    """Execute a job based on its type."""
    if job.job_type == "curate":
        from raasoa.quality.curator import curate
        await curate(session, job.tenant_id)

    elif job.job_type == "compile":
        from raasoa.quality.synthesis import synthesize_all_topics
        await synthesize_all_topics(session, job.tenant_id)

    elif job.job_type == "build_index":
        from raasoa.retrieval.knowledge_index import build_index
        await build_index(session, job.tenant_id)

    elif job.job_type == "retention_cleanup":
        from raasoa.worker.retention import run_retention_cleanup
        await run_retention_cleanup()

    else:
        raise ValueError(f"Unknown job type: {job.job_type}")


async def worker_loop(poll_interval: float = 2.0) -> None:
    """Run the worker loop — processes jobs until stopped."""
    logger.info("Job worker started (poll every %.1fs)", poll_interval)
    while True:
        try:
            had_work = await process_one()
            if not had_work:
                await asyncio.sleep(poll_interval)
        except Exception:
            logger.exception("Worker loop error")
            await asyncio.sleep(5)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    await worker_loop()


if __name__ == "__main__":
    asyncio.run(main())
