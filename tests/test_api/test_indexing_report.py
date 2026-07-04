"""E2E test for the connect-source auto-index indexing report.

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


async def test_indexing_report_surfaces_quality_and_conflicts() -> None:
    """The report an admin sees right after connecting a source must
    reflect real quality findings and real conflicts among the documents
    just synced from that source — this is the entire point of
    auto-indexing on connect."""
    from raasoa.api.sources import _build_indexing_report

    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    doc_a = uuid.uuid4()
    doc_b = uuid.uuid4()

    try:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'ConflictReportTest')"),
                {"id": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'hubspot', 'CRM', '{}'::jsonb)"
                ),
                {"id": source_id, "tid": tenant_id},
            )
            for doc_id, title in [
                (doc_a, "Deal A: closed at 50k"), (doc_b, "Deal A: closed at 45k"),
            ]:
                await session.execute(
                    sql_text(
                        "INSERT INTO documents "
                        "(id, tenant_id, source_id, source_object_id, title, status, "
                        " version, chunk_count, access_count, quality_score) "
                        "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', 1, 1, 0, 0.9)"
                    ),
                    {
                        "id": doc_id, "tid": tenant_id, "sid": source_id,
                        "soid": f"t-{doc_id.hex[:6]}", "title": title,
                    },
                )
            await session.execute(
                sql_text(
                    "INSERT INTO quality_findings (id, document_id, finding_type, severity) "
                    "VALUES (:id, :did, 'low_confidence', 'critical')"
                ),
                {"id": uuid.uuid4(), "did": doc_a},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO conflict_candidates "
                    "(id, tenant_id, document_a_id, document_b_id, "
                    " conflict_type, confidence, status) "
                    "VALUES (:id, :tid, :da, :db, 'value_mismatch', 0.87, 'new')"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "da": doc_a, "db": doc_b},
            )
            await session.commit()

            report = await _build_indexing_report(
                session, tenant_id, source_id, "completed",
                {"synced": 2, "skipped": 0, "errors": []},
            )

            assert report.avg_quality_score == 0.9
            assert report.critical_findings == 1
            assert report.new_conflicts == 1
            assert len(report.top_conflicts) == 1
            assert report.top_conflicts[0].conflict_type == "value_mismatch"
            assert report.top_conflicts[0].document_a_title == "Deal A: closed at 50k"
            assert report.top_conflicts[0].document_b_title == "Deal A: closed at 45k"
    finally:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM conflict_candidates WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM quality_findings WHERE document_id IN (:a, :b)"),
                {"a": doc_a, "b": doc_b},
            )
            await session.execute(
                sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
            )
            await session.commit()
        await engine.dispose()
