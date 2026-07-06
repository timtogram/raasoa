"""Tests for DELETE /v1/sources/{id} (F-046 follow-up).

documents.source_id has no ON DELETE rule, so Postgres refuses to delete
a source that still has documents referencing it. Before this fix, the
endpoint didn't check for this and the unhandled IntegrityError
surfaced as a bare 500. It now checks document count first and returns a
clear 400, and defensively catches the residual race window too.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

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
    yield tid


async def test_delete_source_with_no_documents_succeeds(
    tenant_id: uuid.UUID,
) -> None:
    from raasoa.db import async_session

    source_id = uuid.uuid4()
    async with async_session() as session:
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'custom', 'Empty Source', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        await session.commit()

    async with await _client() as client:
        resp = await client.delete(f"/v1/sources/{source_id}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


async def test_delete_source_with_documents_returns_clean_400_not_500(
    tenant_id: uuid.UUID,
) -> None:
    """Regression: this used to be an unhandled IntegrityError -> 500."""
    from raasoa.db import async_session

    source_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    async with async_session() as session:
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'custom', 'Source With Docs', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " review_status, version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, 'obj-1', 'A Document', 'indexed', "
                " 'published', 1, 0, 0)"
            ),
            {"id": doc_id, "tid": tenant_id, "sid": source_id},
        )
        await session.commit()

    try:
        async with await _client() as client:
            resp = await client.delete(f"/v1/sources/{source_id}")

        assert resp.status_code == 400
        assert "document" in resp.json()["detail"].lower()

        # The source must still exist -- the delete was correctly refused,
        # not partially applied.
        async with async_session() as session:
            row = (
                await session.execute(
                    sql_text("SELECT 1 FROM sources WHERE id = :sid"), {"sid": source_id},
                )
            ).first()
            assert row is not None
    finally:
        async with async_session() as session:
            await session.execute(
                sql_text("DELETE FROM documents WHERE id = :did"), {"did": doc_id},
            )
            await session.execute(
                sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id},
            )
            await session.commit()


async def test_delete_nonexistent_source_returns_404(
    tenant_id: uuid.UUID,
) -> None:
    async with await _client() as client:
        resp = await client.delete(f"/v1/sources/{uuid.uuid4()}")

    assert resp.status_code == 404
