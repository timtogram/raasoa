"""Regression test: the dashboard's quick-connect source form never
persisted sync_interval_minutes, unlike the full POST /v1/sources path
which merges it into connection_config (see api/sources.py's
create_source). A source created via the dashboard therefore never
matched the scheduler's due-query (`connection_config->>'sync_interval_minutes'
IS NOT NULL`) -- it would sync once on connect and then NEVER again
automatically, not even to finish an "incomplete" backlog (a large
SharePoint library, the exact scenario the scheduler's incomplete-status
auto-retry was fixed for). Since the dashboard is the primary UI for
connecting sources, this silently defeated that fix for anyone who
didn't separately know to bypass the dashboard and call the raw API.

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
async def isolated_tenant() -> AsyncGenerator[uuid.UUID, None]:
    from raasoa.db import async_session

    tid = uuid.uuid4()
    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Interval Test Tenant')"),
            {"id": tid},
        )
        await session.commit()

    yield tid

    async with async_session() as session:
        await session.execute(
            sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": tid},
        )
        await session.execute(
            sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid},
        )
        await session.commit()


async def test_sync_interval_minutes_is_persisted_into_connection_config(
    isolated_tenant: uuid.UUID,
) -> None:
    import raasoa.dashboard.routes as dashboard_routes
    from raasoa.db import async_session

    original_tenant = dashboard_routes.DEFAULT_TENANT
    dashboard_routes.DEFAULT_TENANT = str(isolated_tenant)
    try:
        async with await _client() as client:
            resp = await client.post(
                "/dashboard/api/sources",
                json={
                    "source_type": "sharepoint",
                    "name": "Big Library",
                    "config": {"tenant_id_azure": "t", "client_id": "c", "client_secret": "s"},
                    "sync_interval_minutes": 60,
                },
            )
    finally:
        dashboard_routes.DEFAULT_TENANT = original_tenant

    assert resp.status_code == 200
    source_id = resp.json()["id"]

    async with async_session() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT connection_config->>'sync_interval_minutes' AS interval "
                    "FROM sources WHERE id = :sid"
                ),
                {"sid": uuid.UUID(source_id)},
            )
        ).first()
    assert row is not None
    assert row.interval == "60"


async def test_omitted_sync_interval_minutes_leaves_config_unset(
    isolated_tenant: uuid.UUID,
) -> None:
    """Baseline: no regression when the field isn't supplied at all --
    manual-only sources still work exactly as before."""
    import raasoa.dashboard.routes as dashboard_routes
    from raasoa.db import async_session

    original_tenant = dashboard_routes.DEFAULT_TENANT
    dashboard_routes.DEFAULT_TENANT = str(isolated_tenant)
    try:
        async with await _client() as client:
            resp = await client.post(
                "/dashboard/api/sources",
                json={"source_type": "notion", "name": "Manual Only", "config": {"token": "x"}},
            )
    finally:
        dashboard_routes.DEFAULT_TENANT = original_tenant

    assert resp.status_code == 200
    source_id = resp.json()["id"]

    async with async_session() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT connection_config ? 'sync_interval_minutes' AS has_interval "
                    "FROM sources WHERE id = :sid"
                ),
                {"sid": uuid.UUID(source_id)},
            )
        ).first()
    assert row is not None
    assert row.has_interval is False
