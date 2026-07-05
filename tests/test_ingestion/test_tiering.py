"""Tests for tiered indexing logic."""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from raasoa.config import settings
from raasoa.ingestion.tiering import assign_initial_tier
from raasoa.models.document import Document

DATABASE_URL = settings.database_url


def test_initial_tier_is_hot() -> None:
    doc = Document.__new__(Document)
    tier = assign_initial_tier(doc)
    assert tier == "hot"


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


@pytest.mark.skipif(not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}")
class TestRunTieringSweep:
    """Regression for F-023: `interval ':cold_days days'` bound a
    parameter inside a quoted interval literal, which Postgres parses as
    a literal 8-character string — not a valid interval — so this query
    raised `invalid input syntax for type interval` on every run."""

    @pytest.fixture
    async def scenario(self) -> AsyncGenerator[
        tuple[dict[str, object], async_sessionmaker[AsyncSession]], None,
    ]:
        engine = create_async_engine(DATABASE_URL)
        sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        ids: dict[str, object] = {
            "tenant_id": uuid.uuid4(),
            "source_id": uuid.uuid4(),
            "doc_stale_low_quality": uuid.uuid4(),
        }
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'TieringSweepTest')"),
                {"id": ids["tenant_id"]},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources "
                    "(id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'notion', 'Src', '{}'::jsonb)"
                ),
                {"id": ids["source_id"], "tid": ids["tenant_id"]},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " version, chunk_count, access_count, quality_score, index_tier, "
                    " last_accessed_at) "
                    "VALUES (:id, :tid, :sid, :soid, 'Stale Doc', 'indexed', 1, 1, 0, "
                    " 0.1, 'hot', now() - interval '120 days')"
                ),
                {
                    "id": ids["doc_stale_low_quality"], "tid": ids["tenant_id"],
                    "sid": ids["source_id"], "soid": "stale-doc",
                },
            )
            await session.commit()

        yield ids, sessionmaker

        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
            )
            await session.execute(
                sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": ids["tenant_id"]},
            )
            await session.commit()
        await engine.dispose()

    async def test_sweep_runs_without_sql_error_and_demotes_stale_doc(
        self, scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
    ) -> None:
        from raasoa.ingestion.tiering import run_tiering_sweep

        ids, sessionmaker = scenario
        async with sessionmaker() as session:
            stats = await run_tiering_sweep(session)

        assert stats["demoted_to_cold"] >= 1

        async with sessionmaker() as session:
            result = await session.execute(
                sql_text("SELECT index_tier FROM documents WHERE id = :id"),
                {"id": ids["doc_stale_low_quality"]},
            )
            row = result.first()
        assert row is not None
        assert row.index_tier == "cold"
