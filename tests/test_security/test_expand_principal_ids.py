"""E2E tests for group-membership graph expansion.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


@pytest.fixture
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def _add_membership(
    session: AsyncSession, tenant_id: uuid.UUID, member: str, group: str,
) -> None:
    await session.execute(
        sql_text(
            "INSERT INTO principal_memberships "
            "(tenant_id, member_principal_id, group_principal_id) "
            "VALUES (:tid, :m, :g)"
        ),
        {"tid": tenant_id, "m": member, "g": group},
    )


async def test_expand_walks_transitive_memberships(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """user:jane -> group:sales -> group:emea-region: expanding jane's
    principal must surface the whole chain."""
    from raasoa.security.principal import expand_principal_ids

    tenant_id = uuid.uuid4()
    try:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'ExpandTest')"),
                {"id": tenant_id},
            )
            await _add_membership(session, tenant_id, "user:jane", "group:sales")
            await _add_membership(session, tenant_id, "group:sales", "group:emea-region")
            await session.commit()

            result = await expand_principal_ids(session, tenant_id, "user:jane")
        assert set(result) == {"user:jane", "group:sales", "group:emea-region"}
    finally:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM principal_memberships WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
            )
            await session.commit()


async def test_expand_is_cycle_safe(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """group:a -> group:b -> group:a must terminate (max-depth guard), not
    loop forever or blow the stack."""
    from raasoa.security.principal import expand_principal_ids

    tenant_id = uuid.uuid4()
    try:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'CycleTest')"),
                {"id": tenant_id},
            )
            await _add_membership(session, tenant_id, "group:a", "group:b")
            await _add_membership(session, tenant_id, "group:b", "group:a")
            await session.commit()

            result = await expand_principal_ids(session, tenant_id, "group:a")
        assert set(result) == {"group:a", "group:b"}
    finally:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM principal_memberships WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
            )
            await session.commit()


async def test_expand_does_not_cross_tenant_boundary(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two tenants both using 'group:sales' as a principal_id must not
    leak into each other's expansion — this is the exact collision the
    security review flagged (Finding A1)."""
    from raasoa.security.principal import expand_principal_ids

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    try:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'TenantA')"),
                {"id": tenant_a},
            )
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'TenantB')"),
                {"id": tenant_b},
            )
            # Both tenants coincidentally use the same group names.
            await _add_membership(session, tenant_a, "user:jane", "group:sales")
            await _add_membership(session, tenant_a, "group:sales", "group:top-secret-a")
            await _add_membership(session, tenant_b, "user:jane", "group:sales")
            await _add_membership(session, tenant_b, "group:sales", "group:top-secret-b")
            await session.commit()

            result_a = await expand_principal_ids(session, tenant_a, "user:jane")
        assert "group:top-secret-a" in result_a
        assert "group:top-secret-b" not in result_a
    finally:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM principal_memberships WHERE tenant_id IN (:a, :b)"),
                {"a": tenant_a, "b": tenant_b},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                {"a": tenant_a, "b": tenant_b},
            )
            await session.commit()


async def test_expand_no_memberships_returns_self_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from raasoa.security.principal import expand_principal_ids

    tenant_id = uuid.uuid4()
    try:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'LoneTest')"),
                {"id": tenant_id},
            )
            await session.commit()
            result = await expand_principal_ids(session, tenant_id, "user:solo")
        assert result == ["user:solo"]
    finally:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
            )
            await session.commit()
