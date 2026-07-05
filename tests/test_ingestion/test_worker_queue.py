"""E2E tests for the job queue's retry-budget fix (F-024).

Before this fix, `process_one()` always set a failing job straight to
'failed' — a terminal state the claim query's `WHERE status = 'pending'`
never re-selects — so `max_attempts` (referenced in that same claim
query) was never actually honored: every job got exactly one attempt
regardless of its configured budget.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine

from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
    import asyncio

    try:
        async def _check() -> bool:
            engine = create_async_engine(DATABASE_URL)
            try:
                async with engine.connect() as conn:
                    await conn.execute(sql_text("SELECT 1"))
                return True
            finally:
                await engine.dispose()

        return asyncio.run(_check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}",
)


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    """raasoa.db.engine is a loop-bound singleton; pytest-asyncio gives
    each test a fresh event loop, so dispose before/after to avoid a
    pooled connection from a previous test's (now-closed) loop hanging
    or erroring here. See tests/test_api/test_documents_acl.py."""
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def tenant_id() -> AsyncGenerator[uuid.UUID, None]:
    from raasoa.db import async_session

    tid = uuid.uuid4()
    async with async_session() as session:
        # process_one() claims the globally oldest pending job with no
        # tenant scoping — clear any stray pending rows left behind by
        # other tests/dev sessions first, or this test's own job might
        # never be the one actually claimed.
        await session.execute(sql_text("DELETE FROM job_queue WHERE status = 'pending'"))
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'WorkerQueueTest')"),
            {"id": tid},
        )
        await session.commit()

    yield tid

    async with async_session() as session:
        await session.execute(
            sql_text("DELETE FROM job_queue WHERE tenant_id = :tid"), {"tid": tid},
        )
        await session.execute(sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
        await session.commit()


async def _enqueue_unknown_job(tenant_id: uuid.UUID, max_attempts: int) -> uuid.UUID:
    """A job of an unrecognized type always raises in _execute_job —
    a reliable, deterministic way to exercise the failure path."""
    from raasoa.db import async_session

    job_id = uuid.uuid4()
    async with async_session() as session:
        await session.execute(
            sql_text(
                "INSERT INTO job_queue "
                "(id, tenant_id, job_type, payload, priority, max_attempts) "
                "VALUES (:id, :tid, 'nonexistent_job_type', '{}'::jsonb, 0, :max_attempts)"
            ),
            {"id": job_id, "tid": tenant_id, "max_attempts": max_attempts},
        )
        await session.commit()
    return job_id


async def _job_row(job_id: uuid.UUID) -> object:
    from raasoa.db import async_session

    async with async_session() as session:
        result = await session.execute(
            sql_text(
                "SELECT status, attempts, max_attempts FROM job_queue WHERE id = :id"
            ),
            {"id": job_id},
        )
        return result.first()


async def test_failing_job_retries_until_budget_exhausted(
    tenant_id: uuid.UUID,
) -> None:
    from raasoa.worker.queue import process_one

    job_id = await _enqueue_unknown_job(tenant_id, max_attempts=2)

    # Attempt 1: fails, budget remains (attempts=1 < max_attempts=2) -> pending.
    processed = await process_one()
    assert processed is True
    row = await _job_row(job_id)
    assert row.status == "pending"
    assert row.attempts == 1

    # Attempt 2: fails, budget exhausted (attempts=2 >= max_attempts=2) -> failed.
    processed = await process_one()
    assert processed is True
    row = await _job_row(job_id)
    assert row.status == "failed"
    assert row.attempts == 2

    # A failed job is never claimed again.
    processed_again = await process_one()
    assert processed_again is False


async def test_job_with_zero_retry_budget_fails_immediately(
    tenant_id: uuid.UUID,
) -> None:
    from raasoa.worker.queue import process_one

    job_id = await _enqueue_unknown_job(tenant_id, max_attempts=1)

    processed = await process_one()
    assert processed is True
    row = await _job_row(job_id)
    assert row.status == "failed"
    assert row.attempts == 1
