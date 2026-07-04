"""E2E ACL enforcement tests for structured_query().

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

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

Scenario = tuple[dict[str, uuid.UUID], "async_sessionmaker[AsyncSession]"]


@pytest.fixture
async def scenario() -> AsyncGenerator[Scenario, None]:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids = {
        "tenant_id": uuid.uuid4(),
        "src_open": uuid.uuid4(),
        "src_restricted": uuid.uuid4(),
        "doc_open": uuid.uuid4(),
        "doc_restricted": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'StructACLTest')"),
            {"id": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'notion', 'Open', '{}'::jsonb, 'inherit')"
            ),
            {"id": ids["src_open"], "tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'hubspot', 'Restricted', '{}'::jsonb, 'restricted')"
            ),
            {"id": ids["src_restricted"], "tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " version, chunk_count, access_count, quality_score) "
                "VALUES (:id, :tid, :sid, :soid, 'Open Doc', 'indexed', 1, 1, 0, 0.9)"
            ),
            {
                "id": ids["doc_open"], "tid": ids["tenant_id"], "sid": ids["src_open"],
                "soid": f"o-{ids['doc_open'].hex[:6]}",
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " version, chunk_count, access_count, quality_score) "
                "VALUES (:id, :tid, :sid, :soid, 'Restricted Deal Doc', 'indexed', 1, 1, 0, 0.9)"
            ),
            {
                "id": ids["doc_restricted"], "tid": ids["tenant_id"], "sid": ids["src_restricted"],
                "soid": f"r-{ids['doc_restricted'].hex[:6]}",
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO conflict_candidates "
                "(id, tenant_id, document_a_id, document_b_id, conflict_type, confidence, status) "
                "VALUES (:id, :tid, :a, :b, 'value_mismatch', 0.9, 'new')"
            ),
            {
                "id": uuid.uuid4(), "tid": ids["tenant_id"],
                "a": ids["doc_open"], "b": ids["doc_restricted"],
            },
        )
        await session.commit()

    yield ids, sessionmaker

    async with sessionmaker() as session:
        await session.execute(
            sql_text("DELETE FROM conflict_candidates WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
        )
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


async def test_document_count_excludes_restricted_for_stranger(scenario: Scenario) -> None:
    from raasoa.retrieval.structured import structured_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        r_all = await structured_query(session, "how many documents do we have", ids["tenant_id"])
        r_stranger = await structured_query(
            session, "how many documents do we have", ids["tenant_id"],
            principal_ids=["user:stranger"],
        )
    assert r_all.data[0]["total"] == 2
    assert r_stranger.data[0]["total"] == 1


async def test_latest_documents_excludes_restricted_for_stranger(scenario: Scenario) -> None:
    from raasoa.retrieval.structured import structured_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        r_all = await structured_query(session, "latest documents", ids["tenant_id"])
        r_stranger = await structured_query(
            session, "latest documents", ids["tenant_id"], principal_ids=["user:stranger"],
        )
    assert sorted(d["title"] for d in r_all.data) == ["Open Doc", "Restricted Deal Doc"]
    assert sorted(d["title"] for d in r_stranger.data) == ["Open Doc"]


async def test_title_search_fallback_excludes_restricted_for_stranger(
    scenario: Scenario,
) -> None:
    from raasoa.retrieval.structured import structured_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        r_all = await structured_query(session, "Doc", ids["tenant_id"])
        r_stranger = await structured_query(
            session, "Doc", ids["tenant_id"], principal_ids=["user:stranger"],
        )
    assert len(r_all.data) == 2
    assert len(r_stranger.data) == 1


async def test_conflict_summary_visible_when_either_side_visible(scenario: Scenario) -> None:
    """A conflict between an open doc and a restricted doc is still
    countable by a stranger — they can see it exists (the open doc side),
    just not the restricted document's own details elsewhere."""
    from raasoa.retrieval.structured import structured_query

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        r_stranger = await structured_query(
            session, "conflict summary", ids["tenant_id"], principal_ids=["user:stranger"],
        )
    assert sum(d["count"] for d in r_stranger.data) == 1


async def test_conflict_summary_hidden_when_both_sides_restricted(scenario: Scenario) -> None:
    from raasoa.retrieval.structured import structured_query

    ids, sessionmaker = scenario
    doc_restricted_2 = uuid.uuid4()
    async with sessionmaker() as session:
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " version, chunk_count, access_count, quality_score) "
                "VALUES (:id, :tid, :sid, :soid, 'Restricted Deal Doc 2', 'indexed', 1, 1, 0, 0.9)"
            ),
            {
                "id": doc_restricted_2, "tid": ids["tenant_id"], "sid": ids["src_restricted"],
                "soid": f"r2-{doc_restricted_2.hex[:6]}",
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO conflict_candidates "
                "(id, tenant_id, document_a_id, document_b_id, conflict_type, confidence, status) "
                "VALUES (:id, :tid, :a, :b, 'value_mismatch', 0.9, 'new')"
            ),
            {
                "id": uuid.uuid4(), "tid": ids["tenant_id"],
                "a": ids["doc_restricted"], "b": doc_restricted_2,
            },
        )
        await session.commit()

        r_stranger = await structured_query(
            session, "conflict summary", ids["tenant_id"], principal_ids=["user:stranger"],
        )
    # Still only the 1 conflict with a visible side — the fully-restricted
    # conflict must not be counted.
    assert sum(d["count"] for d in r_stranger.data) == 1
