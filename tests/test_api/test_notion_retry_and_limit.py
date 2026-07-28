"""Tests for Notion rate-limit retry/backoff and sync_limit enforcement
(F-046 follow-up).

Before this fix: (1) there was no retry/backoff anywhere in the Notion
sync path -- a single transient 429/5xx during search pagination aborted
the ENTIRE sync immediately, discarding every page already fetched into
``results`` this run; (2) the ``limit`` parameter (documented as "Max
records to pull in the initial sync") was only ever used to set the
per-call page_size, never as an actual cap -- an admin expecting a
bounded sync got Notion's entire matching result set regardless.

Requires a live Postgres for the end-to-end limit-enforcement test.
Skips gracefully when unreachable.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from raasoa.api.sources import _notion_request_with_retry
from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
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


class _FakeResponse:
    def __init__(
        self, status_code: int, payload: dict[str, Any], headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestNotionRequestWithRetry:
    """Pure unit tests, no DB or real sleeping needed (sleep is mocked)."""

    async def test_succeeds_immediately_on_200_no_retry_needed(self) -> None:
        call_count = {"n": 0}

        async def _method(*_a: Any, **_kw: Any) -> _FakeResponse:
            call_count["n"] += 1
            return _FakeResponse(200, {"ok": True})

        resp = await _notion_request_with_retry(_method)
        assert resp.status_code == 200
        assert call_count["n"] == 1

    async def test_retries_on_429_then_succeeds(self) -> None:
        call_count = {"n": 0}

        async def _method(*_a: Any, **_kw: Any) -> _FakeResponse:
            call_count["n"] += 1
            if call_count["n"] < 3:
                return _FakeResponse(429, {"error": "rate limited"})
            return _FakeResponse(200, {"ok": True})

        with patch("raasoa.api.sources.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            resp = await _notion_request_with_retry(_method)

        assert resp.status_code == 200
        assert call_count["n"] == 3
        assert mock_sleep.await_count == 2

    async def test_honors_retry_after_header(self) -> None:
        async def _method(*_a: Any, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(429, {}, headers={"Retry-After": "7"})

        with patch("raasoa.api.sources.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            resp = await _notion_request_with_retry(_method)

        assert resp.status_code == 429  # exhausted retries, final response returned
        # First sleep call must honor the 7-second Retry-After, not the
        # exponential-backoff default (1.0s).
        first_call_delay = mock_sleep.await_args_list[0].args[0]
        assert first_call_delay == 7.0

    async def test_malformed_retry_after_falls_back_to_backoff(self) -> None:
        async def _method(*_a: Any, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(503, {}, headers={"Retry-After": "not-a-number"})

        with patch("raasoa.api.sources.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            resp = await _notion_request_with_retry(_method)

        assert resp.status_code == 503
        first_call_delay = mock_sleep.await_args_list[0].args[0]
        assert first_call_delay == 1.0  # _NOTION_RETRY_DELAY * 2**0

    async def test_non_retryable_status_returns_immediately(self) -> None:
        """A 401/404/etc. must not be retried at all -- only 429/5xx are."""
        call_count = {"n": 0}

        async def _method(*_a: Any, **_kw: Any) -> _FakeResponse:
            call_count["n"] += 1
            return _FakeResponse(401, {"error": "unauthorized"})

        resp = await _notion_request_with_retry(_method)
        assert resp.status_code == 401
        assert call_count["n"] == 1

    async def test_exhausts_all_retries_and_returns_final_failure(self) -> None:
        call_count = {"n": 0}

        async def _method(*_a: Any, **_kw: Any) -> _FakeResponse:
            call_count["n"] += 1
            return _FakeResponse(500, {"error": "still failing"})

        with patch("raasoa.api.sources.asyncio.sleep", new=AsyncMock()):
            resp = await _notion_request_with_retry(_method)

        assert resp.status_code == 500
        assert call_count["n"] == 3  # _NOTION_MAX_RETRIES


@pytest.mark.skipif(not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}")
class TestSyncLimitActuallyBoundsVolume:
    async def test_sync_limit_stops_fetching_once_reached(self) -> None:
        """Regression: the search loop used to keep paging through EVERY
        matching result regardless of `limit`, only using it for
        page_size. A 3-page workspace with limit=1 must stop after the
        first page instead of fetching all 3."""
        from raasoa.api.sources import _sync_notion

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()

        def _page(page_id: str) -> dict[str, Any]:
            return {
                "object": "page",
                "id": page_id,
                "url": f"https://notion.so/{page_id}",
                "created_time": "2026-01-01T00:00:00.000Z",
                "last_edited_time": "2026-06-01T00:00:00.000Z",
                "created_by": {"id": "user-1", "name": "Alice"},
                "parent": {"type": "workspace", "workspace": True},
                "properties": {
                    "title": {"type": "title", "title": [{"plain_text": page_id}]},
                },
            }

        pages_by_cursor = {
            None: {
                "results": [_page("page-1")],
                "has_more": True,
                "next_cursor": "cursor-2",
            },
            "cursor-2": {
                "results": [_page("page-2")],
                "has_more": True,
                "next_cursor": "cursor-3",
            },
            "cursor-3": {
                "results": [_page("page-3")],
                "has_more": False,
                "next_cursor": None,
            },
        }
        get_calls = {"n": 0}

        async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
            cursor = (json or {}).get("start_cursor")
            return _FakeResponse(200, pages_by_cursor[cursor])

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            get_calls["n"] += 1
            return _FakeResponse(
                200,
                {
                    "results": [
                        {
                            "type": "paragraph",
                            "has_children": False,
                            "paragraph": {
                                "rich_text": [
                                    {"plain_text": "Enough body text to pass the length check."},
                                ],
                            },
                        },
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )

        engine = create_async_engine(DATABASE_URL)
        sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                await session.execute(
                    sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Limit Test')"),
                    {"id": tenant_id},
                )
                await session.execute(
                    sql_text(
                        "INSERT INTO sources "
                        "(id, tenant_id, source_type, name, connection_config) "
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
                            return_value=_zero_vector_provider(),
                        ),
                        patch(
                            "raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False,
                        ),
                    ):
                        stats = await _sync_notion(
                            session=session, tenant_id=tenant_id, source_id=source_id,
                            config={"token": "secret-fake-token"}, query="*", limit=1,
                        )

                    # Only the first page's single result was fetched --
                    # the loop stopped as soon as len(results) >= limit,
                    # never requesting cursor-2 or cursor-3.
                    assert stats["found"] == 1
                    assert get_calls["n"] == 1
                finally:
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
                        sql_text("DELETE FROM sync_cursors WHERE source_id = :sid"),
                        {"sid": source_id},
                    )
                    await session.execute(
                        sql_text("DELETE FROM documents WHERE source_id = :sid"),
                        {"sid": source_id},
                    )
                    await session.execute(
                        sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id},
                    )
                    await session.execute(
                        sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
                    )
                    await session.commit()
        finally:
            await engine.dispose()


def _zero_vector_provider() -> Any:
    class _P:
        model_id = "test-stub"
        dimensions = 768

        async def embed(
            self, texts: list[str], *, input_type: str = "search_document"
        ) -> list[list[float]]:
            del input_type
            return [[0.0] * 768 for _ in texts]

    return _P()
