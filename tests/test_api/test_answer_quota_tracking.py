"""Regression test for F-046 follow-up: /v1/answer never persisted its
usage-tracking event.

track_usage() was called inside answer() but nothing ever committed the
session afterward on any of its four return paths, and get_session()'s
dependency doesn't auto-commit on teardown either -- so the INSERT was
silently rolled back every time. Since /v1/answer's own tool description
(in the MCP server) tells agents to *prefer* it over /v1/search, an agent
using exclusively raasoa_answer could call it without limit while never
tripping max_queries_per_month, reopening the exact "switch endpoints to
bypass quota" gap F-020/T-15 was meant to close.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sql_text

from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

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
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


class _ZeroVectorProvider:
    model_id = "test-stub"
    dimensions = 768

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        return [[0.0] * 768 for _ in texts]


async def _client() -> AsyncClient:
    from raasoa.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def tenant_id() -> AsyncGenerator[uuid.UUID, None]:
    from raasoa.db import async_session
    from raasoa.middleware.auth import DEFAULT_TENANT

    tid = DEFAULT_TENANT
    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM tenants WHERE id = :tid"), {"tid": tid},
        )
        if not result.first():
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Default Tenant')"),
                {"id": tid},
            )
            await session.commit()

    before = await _answer_event_count(tid)
    yield tid
    after = await _answer_event_count(tid)
    if after > before:
        async with async_session() as session:
            await session.execute(
                sql_text(
                    "DELETE FROM usage_events WHERE tenant_id = :tid "
                    "AND event_type = 'answer' AND id IN ("
                    "  SELECT id FROM usage_events "
                    "  WHERE tenant_id = :tid AND event_type = 'answer' "
                    "  ORDER BY created_at DESC LIMIT :n"
                    ")"
                ),
                {"tid": tid, "n": after - before},
            )
            await session.commit()


async def _answer_event_count(tenant_id: uuid.UUID) -> int:
    from raasoa.db import async_session

    async with async_session() as session:
        result = await session.execute(
            sql_text(
                "SELECT count(*) AS n FROM usage_events "
                "WHERE tenant_id = :tid AND event_type = 'answer'"
            ),
            {"tid": tenant_id},
        )
        return result.scalar_one()


async def test_answer_persists_usage_event_even_on_honest_refusal(
    tenant_id: uuid.UUID,
) -> None:
    """A query with no matching content in the knowledge base hits the
    'no matching sources' refusal path -- one of the four return points
    in answer() that must still commit the tracked usage event."""
    before = await _answer_event_count(tenant_id)

    with patch(
        "raasoa.api.retrieval.get_embedding_provider",
        return_value=_ZeroVectorProvider(),
    ):
        async with await _client() as client:
            resp = await client.post(
                "/v1/answer",
                json={"query": "completely unrelated nonsense query xyz123"},
            )

    assert resp.status_code == 200
    after = await _answer_event_count(tenant_id)
    assert after == before + 1, (
        "answer()'s usage_events row was not committed -- "
        "track_usage() ran but the session was never committed"
    )
