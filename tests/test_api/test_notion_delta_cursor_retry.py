"""Regression test for a delta-cursor bug found while auditing Notion sync
(F-046 follow-up), distinct from the already-fixed pagination/cursor-uses-
now() bugs.

newest_edited_time (the value written as the new delta cursor) only ever
advances based on whichever pages were successfully processed THIS run.
Once a cursor exists, results come back newest-last_edited_time-first. If
an OLDER page's block-fetch transiently fails (network blip, 429, 5xx --
caught generically and downgraded to title-only content) while a NEWER
page in the same batch succeeds, the cursor still advances to the newer
page's timestamp. On the next sync, the failed page's own last_edited_time
is now <= the new cursor, so it's classified "unchanged" and permanently
skipped -- it will never be retried unless someone edits it again in
Notion, even though the failure was transient and a retry would likely
succeed.

The fix: if ANY page's block-fetch fails this run, the delta cursor does
not advance at all -- every page in the batch (successes included, a
cheap no-op via ingest_file's content-hash dedup) gets re-examined next
sync instead of silently losing the failed one forever.

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


BODY_TEXT = "Enough body text to clear the 50-char threshold comfortably here."


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


async def test_cursor_does_not_advance_when_a_page_fails_to_fetch_blocks(
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
) -> None:
    from raasoa.api.sources import _sync_notion

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()

    # Older page (fails block-fetch) + newer page (succeeds) in the SAME
    # batch -- exactly the scenario that used to silently lose the older
    # page forever.
    search_response = {
        "results": [
            _notion_page("page-old-fails", "Old Page (fails)", "2026-06-01T00:00:00.000Z"),
            _notion_page("page-new-ok", "New Page (ok)", "2026-06-10T00:00:00.000Z"),
        ],
        "has_more": False,
        "next_cursor": None,
    }

    async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(200, search_response)

    async def _get(url: str, **_kw: Any) -> Any:
        if "page-old-fails" in url:
            raise RuntimeError("simulated transient network failure")
        return _FakeResponse(
            200,
            {
                "results": [
                    {
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {"rich_text": [{"plain_text": BODY_TEXT}]},
                    },
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )

    async with sessionmaker_fixture() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Cursor Retry Test')"),
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
                patch("raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False),
            ):
                stats = await _sync_notion(
                    session=session, tenant_id=tenant_id, source_id=source_id,
                    config={"token": "secret-fake-token"}, query="*", limit=50,
                )

            assert stats["delta_complete"] is False

            cursor_row = (
                await session.execute(
                    sql_text(
                        "SELECT delta_token, sync_status FROM sync_cursors "
                        "WHERE source_id = :sid AND source_type = 'notion'"
                    ),
                    {"sid": source_id},
                )
            ).first()
            assert cursor_row is not None
            # Regression: this used to be the newer page's timestamp
            # (2026-06-10...), which would permanently classify
            # "page-old-fails" as unchanged on every future sync.
            assert cursor_row.delta_token is None, (
                f"expected the cursor to stay unset (no prior cursor "
                f"existed) since a page failed this run, got "
                f"{cursor_row.delta_token!r}"
            )
            assert cursor_row.sync_status == "incomplete"

            # Confirm the failed page WAS still ingested (title-only,
            # degraded) -- this fix is about cursor advancement, not
            # about refusing to ingest a degraded fallback.
            titles = {
                r.title
                for r in (
                    await session.execute(
                        sql_text(
                            "SELECT title FROM documents "
                            "WHERE tenant_id = :tid AND source_id = :sid"
                        ),
                        {"tid": tenant_id, "sid": source_id},
                    )
                ).fetchall()
            }
            assert titles == {"Old Page (fails)", "New Page (ok)"}
        finally:
            await _cleanup(session, source_id, tenant_id)


async def test_cursor_advances_normally_when_nothing_fails(
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
) -> None:
    """Baseline: no regression to the happy path -- a batch with zero
    fetch failures still advances the cursor to the max last_edited_time
    seen, exactly as before."""
    from raasoa.api.sources import _sync_notion

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()

    search_response = {
        "results": [
            _notion_page("page-a", "Page A", "2026-06-01T00:00:00.000Z"),
            _notion_page("page-b", "Page B", "2026-06-10T00:00:00.000Z"),
        ],
        "has_more": False,
        "next_cursor": None,
    }

    async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(200, search_response)

    async def _get(url: str, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(
            200,
            {
                "results": [
                    {
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {"rich_text": [{"plain_text": BODY_TEXT}]},
                    },
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )

    async with sessionmaker_fixture() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Cursor Retry Baseline')"),
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
                patch("raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False),
            ):
                stats = await _sync_notion(
                    session=session, tenant_id=tenant_id, source_id=source_id,
                    config={"token": "secret-fake-token"}, query="*", limit=50,
                )

            assert stats["delta_complete"] is True

            from datetime import datetime

            cursor_row = (
                await session.execute(
                    sql_text(
                        "SELECT delta_token, sync_status FROM sync_cursors "
                        "WHERE source_id = :sid AND source_type = 'notion'"
                    ),
                    {"sid": source_id},
                )
            ).first()
            assert cursor_row is not None
            assert cursor_row.sync_status == "completed"
            stored = datetime.fromisoformat(cursor_row.delta_token.replace("Z", "+00:00"))
            expected = datetime.fromisoformat("2026-06-10T00:00:00.000Z".replace("Z", "+00:00"))
            assert stored == expected
        finally:
            await _cleanup(session, source_id, tenant_id)
