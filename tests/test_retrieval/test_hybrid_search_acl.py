"""E2E ACL enforcement tests for hybrid_search/search().

The single most important test suite in the ACL/RBAC buildout: proves the
full enforcement contract end-to-end against real Postgres, not just the
pure predicate-string construction.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import hashlib
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


class _ZeroVectorProvider:
    model_id = "test-stub"
    dimensions = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 768 for _ in texts]


async def _add_doc(
    session: AsyncSession, doc_id: uuid.UUID, source_id: uuid.UUID,
    tenant_id: uuid.UUID, title: str, text_val: str,
) -> None:
    await session.execute(
        sql_text(
            "INSERT INTO documents "
            "(id, tenant_id, source_id, source_object_id, title, status, "
            " version, chunk_count, access_count) "
            "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', 1, 1, 0)"
        ),
        {
            "id": doc_id, "tid": tenant_id, "sid": source_id,
            "soid": f"t-{doc_id.hex[:6]}", "title": title,
        },
    )
    await session.execute(
        sql_text(
            "INSERT INTO chunks "
            "(id, document_id, chunk_index, content_hash, chunk_text, "
            " token_count, embedding, tsv) "
            "VALUES (:id, :did, 0, :hash, :text, 5, :emb, to_tsvector('simple', :text))"
        ),
        {
            "id": uuid.uuid4(), "did": doc_id,
            "hash": hashlib.sha256(text_val.encode()).digest(),
            "text": text_val, "emb": str([0.0] * 768),
        },
    )


@pytest.fixture
async def acl_scenario() -> AsyncGenerator[
    tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]], None,
]:
    """One open source + one restricted source, with a per-document grant
    on one restricted doc and a bare (ungranted) restricted doc.

    Uses a private per-fixture engine throughout (seed, search-under-test,
    and cleanup) rather than the app's global raasoa.db engine — asyncpg
    connections are loop-bound, and pytest-asyncio gives each test
    function a fresh event loop by default, so mixing a private fixture
    engine with the global singleton reliably raises "Event loop is
    closed" once more than one test touches it.
    """
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids = {
        "tenant_id": uuid.uuid4(),
        "src_open_id": uuid.uuid4(),
        "src_restricted_id": uuid.uuid4(),
        "doc_open": uuid.uuid4(),
        "doc_restricted_granted": uuid.uuid4(),
        "doc_restricted_ungranted": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'ACLTest')"),
            {"id": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'notion', 'Open', '{}'::jsonb, 'inherit')"
            ),
            {"id": ids["src_open_id"], "tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'hubspot', 'Restricted CRM', '{}'::jsonb, 'restricted')"
            ),
            {"id": ids["src_restricted_id"], "tid": ids["tenant_id"]},
        )
        await _add_doc(
            session, ids["doc_open"], ids["src_open_id"], ids["tenant_id"],
            "Open Notion Doc", "widget policy content here",
        )
        await _add_doc(
            session, ids["doc_restricted_granted"], ids["src_restricted_id"], ids["tenant_id"],
            "Restricted Deal (granted)", "widget policy content here",
        )
        await _add_doc(
            session, ids["doc_restricted_ungranted"], ids["src_restricted_id"], ids["tenant_id"],
            "Restricted Deal (ungranted)", "widget policy content here",
        )
        await session.execute(
            sql_text(
                "INSERT INTO acl_entries "
                "(id, document_id, principal_type, principal_id, permission) "
                "VALUES (:id, :did, 'user', 'user:jane', 'read')"
            ),
            {"id": uuid.uuid4(), "did": ids["doc_restricted_granted"]},
        )
        await session.commit()

    yield ids, sessionmaker

    async with sessionmaker() as session:
        doc_ids = [
            ids["doc_open"], ids["doc_restricted_granted"], ids["doc_restricted_ungranted"],
        ]
        await session.execute(
            sql_text("DELETE FROM source_acl_grants WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM acl_entries WHERE document_id = ANY(:ids)"), {"ids": doc_ids},
        )
        await session.execute(
            sql_text("DELETE FROM chunks WHERE document_id = ANY(:ids)"), {"ids": doc_ids},
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


async def _titles(
    scenario: tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]],
    **search_kwargs: object,
) -> set[str]:
    from raasoa.retrieval.hybrid_search import search

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await search(
            session=session, query="widget policy", tenant_id=ids["tenant_id"],
            embedding_provider=_ZeroVectorProvider(), top_k=10, **search_kwargs,
        )
    return {r.document_title for r in results if r.document_title}


AclScenario = tuple[dict[str, uuid.UUID], "async_sessionmaker[AsyncSession]"]


async def test_legacy_no_filter_sees_everything(acl_scenario: AclScenario) -> None:
    titles = await _titles(acl_scenario)  # principal_ids not passed -> None
    assert titles == {
        "Open Notion Doc", "Restricted Deal (granted)", "Restricted Deal (ungranted)",
    }


async def test_granted_principal_sees_open_and_own_grant_only(
    acl_scenario: AclScenario,
) -> None:
    titles = await _titles(acl_scenario, principal_ids=["user:jane"])
    assert titles == {"Open Notion Doc", "Restricted Deal (granted)"}


async def test_stranger_sees_only_open_source(acl_scenario: AclScenario) -> None:
    titles = await _titles(acl_scenario, principal_ids=["user:stranger"])
    assert titles == {"Open Notion Doc"}


async def test_empty_principal_ids_fails_closed(acl_scenario: AclScenario) -> None:
    """An authenticated principal with zero resolved grants must see only
    the open source — NOT the unfiltered "everything" behavior. Confirms
    the `is not None` (never truthy) check on principal_ids."""
    titles = await _titles(acl_scenario, principal_ids=[])
    assert titles == {"Open Notion Doc"}


async def test_source_level_grant_covers_all_its_documents(
    acl_scenario: AclScenario,
) -> None:
    """source_acl_grants acts like a virtual ACL row for every document
    from that source — granting the whole restricted source to a group
    surfaces even the previously-ungranted document."""
    ids, sessionmaker = acl_scenario
    async with sessionmaker() as session:
        await session.execute(
            sql_text(
                "INSERT INTO source_acl_grants (tenant_id, source_id, principal_id, permission) "
                "VALUES (:tid, :sid, 'group:all-sales', 'read')"
            ),
            {"tid": ids["tenant_id"], "sid": ids["src_restricted_id"]},
        )
        await session.commit()

    titles = await _titles(acl_scenario, principal_ids=["group:all-sales"])
    assert titles == {
        "Open Notion Doc", "Restricted Deal (granted)", "Restricted Deal (ungranted)",
    }
