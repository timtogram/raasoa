"""Tests for Notion deletion/archival detection (F-046 follow-up).

Unlike SharePoint's delta feed (which explicitly marks removed items
with "@removed"/"deleted"), Notion's search API gives no deletion
signal at all -- a page that's archived, moved to trash, or unshared
from the integration simply stops appearing in search results, with no
indication of why. Before this fix, RAASOA had no mechanism to detect
this: the already-ingested document (and its chunks/embeddings/claims)
stayed status='active' forever, fully retrievable and citable by the
RAG pipeline as if it were still current -- exactly the kind of
staleness the product's own pitch claims to solve.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

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

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        return [[0.0] * 768 for _ in texts]


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _notion_page(page_id: str, title: str, last_edited_time: str) -> dict[str, Any]:
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": last_edited_time,
        "created_by": {"id": "user-1", "name": "Alice"},
        "last_edited_by": {"id": "user-1", "name": "Alice"},
        "parent": {"type": "workspace", "workspace": True},
        "properties": {
            "title": {"type": "title", "title": [{"plain_text": title}]},
        },
    }


BLOCK_CHILDREN_RESPONSE = {
    "results": [
        {
            "type": "paragraph",
            "has_children": False,
            "paragraph": {
                "rich_text": [
                    {"plain_text": "Enough body text to clear the 50-char threshold easily."},
                ],
            },
        },
    ],
    "has_more": False,
    "next_cursor": None,
}


@pytest.fixture
async def sessionmaker_fixture() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def _cleanup(session: AsyncSession, source_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    await session.execute(
        sql_text(
            "DELETE FROM acl_entries WHERE document_id IN "
            "(SELECT id FROM documents WHERE source_id = :sid)"
        ),
        {"sid": source_id},
    )
    await session.execute(
        sql_text(
            "DELETE FROM chunks WHERE document_id IN "
            "(SELECT id FROM documents WHERE source_id = :sid)"
        ),
        {"sid": source_id},
    )
    await session.execute(
        sql_text(
            "DELETE FROM claims WHERE document_id IN "
            "(SELECT id FROM documents WHERE source_id = :sid)"
        ),
        {"sid": source_id},
    )
    await session.execute(
        sql_text("DELETE FROM sync_cursors WHERE source_id = :sid"), {"sid": source_id},
    )
    await session.execute(
        sql_text("DELETE FROM documents WHERE source_id = :sid"), {"sid": source_id},
    )
    await session.execute(
        sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id},
    )
    await session.execute(
        sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
    )
    await session.commit()


class TestMarkNotionPagesDeletedDirectly:
    async def test_document_missing_from_active_set_is_soft_deleted(
        self, sessionmaker_fixture: async_sessionmaker[AsyncSession],
    ) -> None:
        from raasoa.api.sources import _mark_notion_pages_deleted

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()
        doc_id = uuid.uuid4()

        async with sessionmaker_fixture() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Deletion Test')"),
                {"id": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'notion', 'Docs', '{}'::jsonb)"
                ),
                {"id": source_id, "tid": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " review_status, version, chunk_count, access_count) "
                    "VALUES (:id, :tid, :sid, 'notion:page-gone', 'Gone Page', 'indexed', "
                    " 'published', 1, 0, 0)"
                ),
                {"id": doc_id, "tid": tenant_id, "sid": source_id},
            )
            await session.commit()

            try:
                # A non-empty active set that simply doesn't include
                # "page-gone" -- distinct from the empty-set safety guard
                # covered separately below.
                count = await _mark_notion_pages_deleted(
                    session, tenant_id, source_id, active_page_ids={"some-other-page"},
                )
                assert count == 1

                row = (
                    await session.execute(
                        sql_text(
                            "SELECT status, review_status FROM documents WHERE id = :id"
                        ),
                        {"id": doc_id},
                    )
                ).first()
                assert row is not None
                assert row.status == "deleted"
                assert row.review_status == "rejected"
            finally:
                await _cleanup(session, source_id, tenant_id)

    async def test_document_present_in_active_set_is_untouched(
        self, sessionmaker_fixture: async_sessionmaker[AsyncSession],
    ) -> None:
        from raasoa.api.sources import _mark_notion_pages_deleted

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()
        doc_id = uuid.uuid4()

        async with sessionmaker_fixture() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Deletion Test 2')"),
                {"id": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'notion', 'Docs', '{}'::jsonb)"
                ),
                {"id": source_id, "tid": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " review_status, version, chunk_count, access_count) "
                    "VALUES (:id, :tid, :sid, 'notion:page-alive', 'Alive Page', 'indexed', "
                    " 'published', 1, 0, 0)"
                ),
                {"id": doc_id, "tid": tenant_id, "sid": source_id},
            )
            await session.commit()

            try:
                count = await _mark_notion_pages_deleted(
                    session, tenant_id, source_id, active_page_ids={"page-alive"},
                )
                assert count == 0

                row = (
                    await session.execute(
                        sql_text("SELECT status FROM documents WHERE id = :id"), {"id": doc_id},
                    )
                ).first()
                assert row is not None
                assert row.status == "indexed"
            finally:
                await _cleanup(session, source_id, tenant_id)

    async def test_empty_active_set_never_mass_deletes(
        self, sessionmaker_fixture: async_sessionmaker[AsyncSession],
    ) -> None:
        """A transient empty search response must not be mistaken for
        "everything was deleted" -- the guard returns 0 without touching
        any row."""
        from raasoa.api.sources import _mark_notion_pages_deleted

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()
        doc_id = uuid.uuid4()

        async with sessionmaker_fixture() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Empty Guard Test')"),
                {"id": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'notion', 'Docs', '{}'::jsonb)"
                ),
                {"id": source_id, "tid": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " review_status, version, chunk_count, access_count) "
                    "VALUES (:id, :tid, :sid, 'notion:page-x', 'Page X', 'indexed', "
                    " 'published', 1, 0, 0)"
                ),
                {"id": doc_id, "tid": tenant_id, "sid": source_id},
            )
            await session.commit()

            try:
                count = await _mark_notion_pages_deleted(
                    session, tenant_id, source_id, active_page_ids=set(),
                )
                assert count == 0

                row = (
                    await session.execute(
                        sql_text("SELECT status FROM documents WHERE id = :id"), {"id": doc_id},
                    )
                ).first()
                assert row is not None
                assert row.status == "indexed"
            finally:
                await _cleanup(session, source_id, tenant_id)


class TestSyncNotionEndToEndDeletionDetection:
    async def test_page_absent_from_a_later_full_sync_gets_soft_deleted(
        self, sessionmaker_fixture: async_sessionmaker[AsyncSession],
    ) -> None:
        from raasoa.api.sources import _sync_notion

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()

        first_sync_response = {
            "results": [
                _notion_page("page-keep", "Keeper", "2026-06-01T00:00:00.000Z"),
                _notion_page("page-remove", "Removed Later", "2026-06-01T00:00:00.000Z"),
            ],
            "has_more": False,
            "next_cursor": None,
        }
        # Second sync: "page-remove" no longer appears at all -- as if
        # archived/trashed/unshared in Notion.
        second_sync_response = {
            "results": [
                _notion_page("page-keep", "Keeper", "2026-06-02T00:00:00.000Z"),
            ],
            "has_more": False,
            "next_cursor": None,
        }

        call_state = {"n": 0}

        async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
            call_state["n"] += 1
            payload = first_sync_response if call_state["n"] == 1 else second_sync_response
            return _FakeResponse(200, payload)

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, BLOCK_CHILDREN_RESPONSE)

        async with sessionmaker_fixture() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'E2E Deletion Test')"),
                {"id": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'notion', 'Docs', '{}'::jsonb)"
                ),
                {"id": source_id, "tid": tenant_id},
            )
            await session.commit()

            try:
                with (
                    patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_post)),
                    patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)),
                    patch(
                        "raasoa.providers.factory.get_embedding_provider",
                        return_value=_ZeroVectorProvider(),
                    ),
                    patch(
                        "raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False,
                    ),
                ):
                    stats1 = await _sync_notion(
                        session=session, tenant_id=tenant_id, source_id=source_id,
                        config={"token": "secret-fake-token"}, query="*", limit=50,
                    )
                    assert stats1["synced"] == 2

                    stats2 = await _sync_notion(
                        session=session, tenant_id=tenant_id, source_id=source_id,
                        config={"token": "secret-fake-token"}, query="*", limit=50,
                    )

                assert stats2["deleted"] == 1

                rows = (
                    await session.execute(
                        sql_text(
                            "SELECT title, status FROM documents "
                            "WHERE tenant_id = :tid AND source_id = :sid ORDER BY title"
                        ),
                        {"tid": tenant_id, "sid": source_id},
                    )
                ).fetchall()
                by_title = {r.title: r.status for r in rows}
                assert by_title["Keeper"] == "indexed"
                assert by_title["Removed Later"] == "deleted"
            finally:
                await _cleanup(session, source_id, tenant_id)

    async def test_scoped_query_does_not_trigger_deletion_detection(
        self, sessionmaker_fixture: async_sessionmaker[AsyncSession],
    ) -> None:
        """A narrower/filtered search's results say nothing about pages
        outside that query's scope -- running deletion detection against
        them would wrongly mark unrelated documents as deleted."""
        from raasoa.api.sources import _sync_notion

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()
        pre_existing_doc_id = uuid.uuid4()

        scoped_search_response = {
            "results": [
                _notion_page("page-match", "Matches Query", "2026-06-01T00:00:00.000Z"),
            ],
            "has_more": False,
            "next_cursor": None,
        }

        async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
            assert json is not None and json.get("query") == "budget"
            return _FakeResponse(200, scoped_search_response)

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, BLOCK_CHILDREN_RESPONSE)

        async with sessionmaker_fixture() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Scoped Query Test')"),
                {"id": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'notion', 'Docs', '{}'::jsonb)"
                ),
                {"id": source_id, "tid": tenant_id},
            )
            # A pre-existing document NOT part of the "budget" query's
            # results at all -- it must survive a scoped sync untouched.
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " review_status, version, chunk_count, access_count) "
                    "VALUES (:id, :tid, :sid, 'notion:page-unrelated', 'Unrelated Page', "
                    " 'indexed', 'published', 1, 0, 0)"
                ),
                {"id": pre_existing_doc_id, "tid": tenant_id, "sid": source_id},
            )
            await session.commit()

            try:
                with (
                    patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_post)),
                    patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)),
                    patch(
                        "raasoa.providers.factory.get_embedding_provider",
                        return_value=_ZeroVectorProvider(),
                    ),
                    patch(
                        "raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False,
                    ),
                ):
                    stats = await _sync_notion(
                        session=session, tenant_id=tenant_id, source_id=source_id,
                        config={"token": "secret-fake-token"}, query="budget", limit=50,
                    )

                assert stats["deleted"] == 0

                row = (
                    await session.execute(
                        sql_text("SELECT status FROM documents WHERE id = :id"),
                        {"id": pre_existing_doc_id},
                    )
                ).first()
                assert row is not None
                assert row.status == "indexed", (
                    "a scoped query must never trigger deletion detection "
                    "against documents outside its own result set"
                )
            finally:
                await _cleanup(session, source_id, tenant_id)
