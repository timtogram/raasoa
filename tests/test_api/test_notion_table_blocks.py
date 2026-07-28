"""End-to-end regression test: a Notion table's cell data must survive
the full sync pipeline into the ingested document's content.

table_row's cell content lives under "cells" (a list of lists of
rich-text objects), not "rich_text" like every other Notion block type.
Before this fix, _notion_block_to_text's generic rich_text-based
extraction saw an empty list for every table row and produced an empty
string -- so a page's entire table (all cell content) was invisible to
RAG, with no placeholder or signal that a table had ever existed.

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


PAGE_WITH_TABLE = {
    "object": "page",
    "id": "page-with-table",
    "url": "https://notion.so/page-with-table",
    "created_time": "2026-01-01T00:00:00.000Z",
    "last_edited_time": "2026-06-01T00:00:00.000Z",
    "created_by": {"id": "user-1", "name": "Alice"},
    "last_edited_by": {"id": "user-1", "name": "Alice"},
    "parent": {"type": "workspace", "workspace": True},
    "properties": {
        "title": {"type": "title", "title": [{"plain_text": "Budget Overview"}]},
    },
}

SEARCH_RESPONSE = {
    "results": [PAGE_WITH_TABLE],
    "has_more": False,
    "next_cursor": None,
}

# The page's own children: one paragraph, then a table block with
# has_children=True (its rows are fetched via a SEPARATE children call).
PAGE_BLOCKS_RESPONSE = {
    "results": [
        {
            "type": "paragraph",
            "has_children": False,
            "paragraph": {"rich_text": [{"plain_text": "See the budget breakdown below."}]},
        },
        {
            "id": "table-block-1",
            "type": "table",
            "has_children": True,
            "table": {"table_width": 2, "has_column_header": True},
        },
    ],
    "has_more": False,
    "next_cursor": None,
}

# The table block's children: header row + one data row.
TABLE_ROWS_RESPONSE = {
    "results": [
        {
            "type": "table_row",
            "has_children": False,
            "table_row": {
                "cells": [
                    [{"plain_text": "Category"}],
                    [{"plain_text": "Amount"}],
                ],
            },
        },
        {
            "type": "table_row",
            "has_children": False,
            "table_row": {
                "cells": [
                    [{"plain_text": "Travel"}],
                    [{"plain_text": "42000 EUR"}],
                ],
            },
        },
    ],
    "has_more": False,
    "next_cursor": None,
}


async def _make_get_mock() -> AsyncMock:
    """Returns page-level blocks for the page's own children call, and
    table rows for the table block's children call -- distinguished by
    which block id is in the URL."""

    async def _get(url: str, **_kw: Any) -> _FakeResponse:
        assert "blocks" in url and "children" in url
        if "table-block-1" in url:
            return _FakeResponse(200, TABLE_ROWS_RESPONSE)
        return _FakeResponse(200, PAGE_BLOCKS_RESPONSE)

    return AsyncMock(side_effect=_get)


@pytest.fixture
async def sessionmaker_fixture() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def test_table_cell_data_reaches_the_ingested_document(
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
) -> None:
    from raasoa.api.sources import _sync_notion

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()

    async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
        assert "search" in url
        return _FakeResponse(200, SEARCH_RESPONSE)

    async with sessionmaker_fixture() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Table Test')"),
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

            assert stats["synced"] == 1

            chunk_text = (
                await session.execute(
                    sql_text(
                        "SELECT string_agg(c.chunk_text, ' ') AS full_text "
                        "FROM chunks c "
                        "JOIN documents d ON d.id = c.document_id "
                        "WHERE d.tenant_id = :tid AND d.source_id = :sid"
                    ),
                    {"tid": tenant_id, "sid": source_id},
                )
            ).scalar_one()

            assert chunk_text is not None
            # Table header and data cells must both be present -- this is
            # what used to be silently dropped entirely.
            assert "Category" in chunk_text
            assert "Amount" in chunk_text
            assert "Travel" in chunk_text
            assert "42000 EUR" in chunk_text
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
