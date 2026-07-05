"""End-to-end tests for the Notion connector's sync logic (F-022).

No real Notion workspace is available in this environment, so the Notion
Search API is mocked at the httpx.AsyncClient.post level — everything
downstream (pagination loop, ingestion, delta cursor) runs for real against
a live Postgres.

Covers two bugs:
  a. Pagination: a workspace with more than one page of search results
     (Notion's ``has_more``/``next_cursor`` fields) must have ALL pages
     ingested, not just the first.
  b. Delta cursor: after a sync, the stored delta token must be the max
     ``last_edited_time`` actually seen among synced pages — not
     server wall-clock "now" — so that edits made between the search call
     and the cursor write aren't silently skipped on the next sync.

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


class _DistinctVectorProvider:
    """Stub embedding provider — avoids a real Ollama call entirely, so the
    globally-scoped httpx.AsyncClient.post patch below only ever needs to
    handle Notion API calls, not embedding calls too.

    Deliberately returns a DIFFERENT vector per call rather than an
    all-zero vector for every text, keeping this test representative of
    real (non-colliding) pages.
    """

    model_id = "test-stub"
    dimensions = 768

    def __init__(self) -> None:
        self._counter = 0

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        vectors = []
        for _ in texts:
            self._counter += 1
            vec = [0.0] * 768
            vec[self._counter % 768] = 1.0
            vectors.append(vec)
        return vectors


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


def _notion_page(
    page_id: str, title: str, last_edited_time: str,
) -> dict[str, Any]:
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
            "title": {
                "type": "title",
                "title": [{"plain_text": title}],
            },
        },
    }


# Page 1: has_more=True, next_cursor set. Page 2: has_more=False, terminates
# the loop. Together these exceed what a single 100-page call would return,
# proving the pagination loop follows next_cursor/has_more.
SEARCH_PAGE_1 = {
    "results": [
        _notion_page("page-1", "First Page Doc", "2026-06-01T00:00:00.000Z"),
    ],
    "has_more": True,
    "next_cursor": "cursor-abc",
}

# The second page has the MAX last_edited_time — the stored delta cursor
# must reflect this value, not wall-clock "now".
SEARCH_PAGE_2 = {
    "results": [
        _notion_page("page-2", "Second Page Doc", "2026-06-15T12:30:00.000Z"),
    ],
    "has_more": False,
    "next_cursor": None,
}

# Enough content so pages aren't skipped for being too short (<50 chars).
BLOCK_CHILDREN_RESPONSE = {
    "results": [
        {
            "type": "paragraph",
            "has_children": False,
            "paragraph": {
                "rich_text": [
                    {
                        "plain_text": (
                            "This is a sufficiently long paragraph of body "
                            "text so the page is not skipped as too short."
                        ),
                    },
                ],
            },
        },
    ],
    "has_more": False,
    "next_cursor": None,
}


async def _make_post_mock() -> AsyncMock:
    """Mocks Notion's POST /v1/search — returns page 1 then page 2,
    following next_cursor across two separate calls."""
    call_count = {"n": 0}

    async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
        assert "search" in url
        call_count["n"] += 1
        if call_count["n"] == 1:
            assert json is not None and "start_cursor" not in json
            return _FakeResponse(200, SEARCH_PAGE_1)
        assert json is not None and json.get("start_cursor") == "cursor-abc"
        return _FakeResponse(200, SEARCH_PAGE_2)

    return AsyncMock(side_effect=_post)


async def _make_get_mock() -> AsyncMock:
    """Mocks Notion's GET /v1/blocks/{id}/children for page content."""

    async def _get(url: str, **_kw: Any) -> _FakeResponse:
        assert "blocks" in url and "children" in url
        return _FakeResponse(200, BLOCK_CHILDREN_RESPONSE)

    return AsyncMock(side_effect=_get)


@pytest.fixture
async def private_sessionmaker() -> AsyncSession:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()


async def test_notion_sync_paginates_and_records_max_last_edited_time(
    private_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """F-022: both pages (across two search calls linked by next_cursor)
    must be ingested, and the stored delta cursor must be the max
    last_edited_time seen (2026-06-15T12:30:00+00:00) — not wall-clock
    "now"."""
    from raasoa.api.sources import _sync_notion

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()

    async with private_sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Notion Test')"),
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
                patch("httpx.AsyncClient.post", await _make_post_mock()),
                patch("httpx.AsyncClient.get", await _make_get_mock()),
                patch(
                    "raasoa.providers.factory.get_embedding_provider",
                    return_value=_DistinctVectorProvider(),
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

            # Both pages, across both search calls, were ingested.
            assert stats["found"] == 2
            assert stats["synced"] == 2

            docs = await session.execute(
                sql_text(
                    "SELECT title FROM documents "
                    "WHERE tenant_id = :tid AND source_id = :sid ORDER BY title"
                ),
                {"tid": tenant_id, "sid": source_id},
            )
            titles = {r.title for r in docs.fetchall()}
            assert titles == {"First Page Doc", "Second Page Doc"}

            # Delta cursor reflects the MAX last_edited_time actually seen
            # (page 2's timestamp), not wall-clock now(). Compare via
            # datetime parsing so Z-vs-+00:00 formatting differences don't
            # cause a spurious mismatch.
            from datetime import datetime

            cursor_result = await session.execute(
                sql_text(
                    "SELECT delta_token FROM sync_cursors "
                    "WHERE source_id = :sid AND source_type = 'notion'"
                ),
                {"sid": source_id},
            )
            cursor_row = cursor_result.first()
            assert cursor_row is not None
            stored = datetime.fromisoformat(cursor_row.delta_token.replace("Z", "+00:00"))
            expected = datetime.fromisoformat("2026-06-15T12:30:00.000Z".replace("Z", "+00:00"))
            assert stored == expected, (
                f"expected delta cursor to be the max last_edited_time seen "
                f"({expected.isoformat()}), got {stored.isoformat()} — looks like "
                "wall-clock now() was stored instead"
            )

            # Sanity: the stored cursor must NOT be "now" (which would be
            # far later than the fixture's fixed 2026-06-15 timestamp).
            from datetime import UTC

            now = datetime.now(UTC)
            assert (now - stored).total_seconds() > 3600, (
                "delta cursor looks like wall-clock now(), not the max "
                "last_edited_time seen among synced pages"
            )
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


async def test_notion_sync_requires_token() -> None:
    from raasoa.api.sources import _sync_notion

    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with sessionmaker() as session:
            stats = await _sync_notion(
                session=session,
                tenant_id=uuid.uuid4(),
                source_id=uuid.uuid4(),
                config={},
                query="*",
                limit=50,
            )
        assert stats["status"] == "error"
        assert "token" in stats["message"].lower()
    finally:
        await engine.dispose()
