"""Tests for tenant source-limit quota enforcement (F-046 follow-up).

max_sources defaulted to 1, which blocked provisioning the second of
even two needed connectors (e.g. Notion + SharePoint) via the documented
POST /v1/sources path. Separately, the dashboard's own
POST /dashboard/api/sources did a raw INSERT with no quota check at
all -- the exact same limit was enforced on one door and bypassed on the
other.

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
    """A fresh tenant, not the shared DEFAULT_TENANT other tests reuse --
    quota tests need to control exactly how many sources exist."""
    from raasoa.db import async_session

    tid = uuid.uuid4()
    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Quota Test Tenant')"),
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


async def test_new_tenant_defaults_to_max_sources_ten(
    isolated_tenant: uuid.UUID,
) -> None:
    """Regression: this used to be 1, blocking the second of exactly the
    two connectors (Notion + SharePoint) a real deployment needs."""
    from raasoa.db import async_session

    async with async_session() as session:
        row = (
            await session.execute(
                sql_text("SELECT max_sources FROM tenants WHERE id = :tid"),
                {"tid": isolated_tenant},
            )
        ).first()
    assert row is not None
    assert row.max_sources == 10


class TestDashboardCreateSourceEnforcesQuota:
    """Regression: the dashboard's own create-source route used to do a
    raw INSERT with zero quota check, bypassing the exact limit
    POST /v1/sources enforced."""

    async def test_dashboard_rejects_source_past_the_limit(
        self, isolated_tenant: uuid.UUID,
    ) -> None:
        from raasoa.db import async_session

        # Force the isolated tenant's limit down to 1 so a single
        # existing source already saturates it, then patch DEFAULT_TENANT
        # to point at it for the duration of this test (dashboard routes
        # always operate on DEFAULT_TENANT, not a caller-supplied one).
        async with async_session() as session:
            await session.execute(
                sql_text(
                    "UPDATE tenants SET max_sources = 1 WHERE id = :tid"
                ),
                {"tid": isolated_tenant},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'custom', 'Existing Source', '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "tid": isolated_tenant},
            )
            await session.commit()

        import raasoa.dashboard.routes as dashboard_routes

        original_tenant = dashboard_routes.DEFAULT_TENANT
        dashboard_routes.DEFAULT_TENANT = str(isolated_tenant)
        try:
            async with await _client() as client:
                resp = await client.post(
                    "/dashboard/api/sources",
                    json={"source_type": "notion", "name": "Second Source"},
                )
        finally:
            dashboard_routes.DEFAULT_TENANT = original_tenant

        assert resp.status_code == 429
        assert "limit" in resp.json()["detail"].lower()

        async with async_session() as session:
            count = (
                await session.execute(
                    sql_text(
                        "SELECT count(*) AS n FROM sources WHERE tenant_id = :tid"
                    ),
                    {"tid": isolated_tenant},
                )
            ).scalar_one()
        assert count == 1, "rejected source must not have been inserted anyway"

    async def test_dashboard_allows_source_under_the_limit(
        self, isolated_tenant: uuid.UUID,
    ) -> None:
        import raasoa.dashboard.routes as dashboard_routes

        original_tenant = dashboard_routes.DEFAULT_TENANT
        dashboard_routes.DEFAULT_TENANT = str(isolated_tenant)
        try:
            async with await _client() as client:
                resp = await client.post(
                    "/dashboard/api/sources",
                    json={"source_type": "notion", "name": "First Source"},
                )
        finally:
            dashboard_routes.DEFAULT_TENANT = original_tenant

        assert resp.status_code == 200
        assert resp.json()["status"] == "created"
