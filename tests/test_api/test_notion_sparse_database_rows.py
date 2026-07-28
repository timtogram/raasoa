"""Regression test for F-046 follow-up: Notion database rows with a short
title, empty body, and no interesting content beyond their properties used
to be silently dropped entirely.

_sync_notion checked ``len(content.strip()) < 50`` -- the block-derived
body text alone -- BEFORE folding title/status/tags/author into
meta_header. A database row (task tracker item, CRM record) commonly has
a short title and zero page body, with all its real information living
in properties. Checking the block text alone meant such rows never made
it into the corpus at all, for a database-heavy workspace that could mean
most rows.

The fix checks the length threshold on the fully assembled file content
(title + meta_header + body) instead, so a database row with substantial
property data (a status, tags, etc.) passes even with an empty body.

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


# A database row: parent is a database, short title, a "status" property
# (like a real task-tracker row would have), and NO body blocks at all --
# exactly the shape that used to be silently dropped.
DATABASE_ROW_PAGE = {
    "object": "page",
    "id": "row-1",
    "url": "https://notion.so/row-1",
    "created_time": "2026-01-01T00:00:00.000Z",
    "last_edited_time": "2026-06-01T00:00:00.000Z",
    "created_by": {"id": "user-1", "name": "Alice"},
    "last_edited_by": {"id": "user-1", "name": "Alice"},
    "parent": {"type": "database_id", "database_id": "db-1"},
    "properties": {
        "Name": {
            "type": "title",
            "title": [{"plain_text": "Fix login bug"}],
        },
        "Status": {
            "type": "status",
            "status": {"name": "In Progress"},
        },
    },
}

SEARCH_RESPONSE = {
    "results": [DATABASE_ROW_PAGE],
    "has_more": False,
    "next_cursor": None,
}

# Zero body blocks -- this is the point: a database row commonly has no
# page content at all, only properties.
EMPTY_BLOCKS_RESPONSE = {"results": [], "has_more": False, "next_cursor": None}


async def _make_post_mock() -> AsyncMock:
    async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
        assert "search" in url
        return _FakeResponse(200, SEARCH_RESPONSE)

    return AsyncMock(side_effect=_post)


async def _make_get_mock() -> AsyncMock:
    async def _get(url: str, **_kw: Any) -> _FakeResponse:
        assert "blocks" in url and "children" in url
        return _FakeResponse(200, EMPTY_BLOCKS_RESPONSE)

    return AsyncMock(side_effect=_get)


@pytest.fixture
async def sessionmaker_fixture() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def test_sparse_body_database_row_with_properties_is_not_dropped(
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
) -> None:
    from raasoa.api.sources import _sync_notion

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()

    async with sessionmaker_fixture() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Sparse Row Test')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'notion', 'Tracker', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        await session.commit()

        try:
            with (
                patch("httpx.AsyncClient.post", await _make_post_mock()),
                patch("httpx.AsyncClient.get", await _make_get_mock()),
                patch(
                    "raasoa.providers.factory.get_embedding_provider",
                    return_value=_ZeroVectorProvider(),
                ),
                patch("raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False),
            ):
                stats = await _sync_notion(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    config={"token": "secret-fake-token"},
                    query="*",
                    limit=50,
                )

            # Regression: this used to be stats["skipped"] == 1,
            # stats["synced"] == 0 -- the row was silently dropped.
            assert stats["synced"] == 1, (
                f"expected the database row to be ingested despite an "
                f"empty body, got stats={stats}"
            )
            assert stats["skipped"] == 0

            doc = (
                await session.execute(
                    sql_text(
                        "SELECT title, doc_metadata FROM documents "
                        "WHERE tenant_id = :tid AND source_id = :sid"
                    ),
                    {"tid": tenant_id, "sid": source_id},
                )
            ).first()
            assert doc is not None
            assert doc.title == "Fix login bug"
            assert doc.doc_metadata.get("status") == "In Progress"
        finally:
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


async def test_truly_empty_page_with_no_properties_is_still_skipped(
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
) -> None:
    """The threshold must still filter genuinely empty/junk pages.

    Note: real Notion pages always carry a last_edited_time, and most
    have a resolvable created_by name/id -- once folded into
    meta_header, a title plus that minimal metadata alone is usually
    enough to clear 50 chars on its own now. That's an intentional,
    acceptable side effect of the fix (a near-blank page ends up with a
    small but harmless document, instead of the alternative of losing
    real database rows with actual property data). This test omits even
    that guaranteed metadata to confirm the threshold still does
    something when truly nothing useful is present at all.
    """
    from raasoa.api.sources import _sync_notion

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()

    blank_page = {
        "object": "page",
        "id": "blank-1",
        "url": "https://notion.so/blank-1",
        "created_time": "2026-01-01T00:00:00.000Z",
        "parent": {"type": "workspace", "workspace": True},
        "properties": {
            "title": {"type": "title", "title": [{"plain_text": "X"}]},
        },
    }
    search_response = {"results": [blank_page], "has_more": False, "next_cursor": None}

    async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(200, search_response)

    async def _get(url: str, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(200, EMPTY_BLOCKS_RESPONSE)

    async with sessionmaker_fixture() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Blank Page Test')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'notion', 'Blank', '{}'::jsonb)"
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
                patch("raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False),
            ):
                stats = await _sync_notion(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    config={"token": "secret-fake-token"},
                    query="*",
                    limit=50,
                )

            assert stats["synced"] == 0
            assert stats["skipped"] == 1
        finally:
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
