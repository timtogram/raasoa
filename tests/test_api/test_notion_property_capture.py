"""Tests for expanded Notion database property capture (F-046 follow-up).

Only status/tags/author/last_edited_by/last_edited_time/parent_path ever
reached the ingested file's searchable text before this fix -- everything
else (custom select fields, people/date/url/rich_text properties, and
number/checkbox/email/phone_number/formula which weren't even extracted
into doc_metadata at all) was invisible to semantic/hybrid search and RAG
answers, reachable only via exact-match structured filtering (or not
reachable at all for the unextracted types).

Requires a live Postgres for the end-to-end test. Skips gracefully when
unreachable.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from raasoa.api.sources import _notion_metadata
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


class TestNotionMetadataCustomProperties:
    """Pure unit tests on _notion_metadata -- no mocking or DB needed."""

    def _base_page(self, properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "created_time": "2026-01-01T00:00:00.000Z",
            "last_edited_time": "2026-06-01T00:00:00.000Z",
            "created_by": {"id": "user-1", "name": "Alice"},
            "parent": {"type": "workspace", "workspace": True},
            "properties": properties,
        }

    def test_select_property_captured_into_custom_properties(self) -> None:
        page = self._base_page({
            "Priority": {"type": "select", "select": {"name": "High"}},
        })
        meta = _notion_metadata(page)
        assert meta["priority"] == "High"  # existing flattened key, unchanged
        assert meta["custom_properties"]["Priority"] == "High"

    def test_people_property_captured_into_custom_properties(self) -> None:
        page = self._base_page({
            "Assignee": {
                "type": "people",
                "people": [{"name": "Bob"}, {"name": "Carol"}],
            },
        })
        meta = _notion_metadata(page)
        assert meta["property_assignee"] == "Bob, Carol"
        assert meta["custom_properties"]["Assignee"] == "Bob, Carol"

    def test_date_property_captured_into_custom_properties(self) -> None:
        page = self._base_page({
            "Due Date": {"type": "date", "date": {"start": "2026-08-01"}},
        })
        meta = _notion_metadata(page)
        assert meta["date_due date"] == "2026-08-01"
        assert meta["custom_properties"]["Due Date"] == "2026-08-01"

    def test_url_property_captured_into_custom_properties(self) -> None:
        page = self._base_page({
            "Website": {"type": "url", "url": "https://example.com"},
        })
        meta = _notion_metadata(page)
        assert meta["custom_properties"]["Website"] == "https://example.com"

    def test_number_property_now_captured_at_all(self) -> None:
        """Regression: number properties were completely unextracted
        before this fix -- not even present in doc_metadata."""
        page = self._base_page({
            "Budget": {"type": "number", "number": 42000},
        })
        meta = _notion_metadata(page)
        assert meta["number_budget"] == 42000
        assert meta["custom_properties"]["Budget"] == "42000"

    def test_checkbox_property_now_captured_including_false(self) -> None:
        page = self._base_page({
            "Completed": {"type": "checkbox", "checkbox": False},
        })
        meta = _notion_metadata(page)
        assert meta["checkbox_completed"] is False
        assert meta["custom_properties"]["Completed"] == "No"

    def test_email_property_now_captured_at_all(self) -> None:
        page = self._base_page({
            "Contact": {"type": "email", "email": "person@example.com"},
        })
        meta = _notion_metadata(page)
        assert meta["email_contact"] == "person@example.com"
        assert meta["custom_properties"]["Contact"] == "person@example.com"

    def test_phone_number_property_now_captured_at_all(self) -> None:
        page = self._base_page({
            "Phone": {"type": "phone_number", "phone_number": "+1 555 0100"},
        })
        meta = _notion_metadata(page)
        assert meta["phone_phone"] == "+1 555 0100"
        assert meta["custom_properties"]["Phone"] == "+1 555 0100"

    def test_formula_property_scalar_types_captured(self) -> None:
        page = self._base_page({
            "Days Left": {"type": "formula", "formula": {"type": "number", "number": 5}},
        })
        meta = _notion_metadata(page)
        assert meta["formula_days left"] == 5
        assert meta["custom_properties"]["Days Left"] == "5"

    def test_relation_and_rollup_are_deliberately_not_captured(self) -> None:
        """Documented scope decision, not an oversight -- relation values
        are bare page UUIDs with no human-readable text without an extra
        API call per reference; rollup needs recursive unwrapping of a
        possibly-array-typed nested value."""
        page = self._base_page({
            "Related Tasks": {"type": "relation", "relation": [{"id": "abc-123"}]},
            "Total Cost": {"type": "rollup", "rollup": {"type": "number", "number": 100}},
        })
        meta = _notion_metadata(page)
        assert "custom_properties" not in meta or not meta["custom_properties"]

    def test_multiple_custom_properties_all_present(self) -> None:
        page = self._base_page({
            "Priority": {"type": "select", "select": {"name": "High"}},
            "Budget": {"type": "number", "number": 5000},
            "Approved": {"type": "checkbox", "checkbox": True},
        })
        meta = _notion_metadata(page)
        assert meta["custom_properties"] == {
            "Priority": "High",
            "Budget": "5000",
            "Approved": "Yes",
        }


@pytest.mark.skipif(not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}")
class TestCustomPropertiesReachTheIngestedDocument:
    async def test_custom_property_text_ends_up_in_a_searchable_chunk(self) -> None:
        from raasoa.api.sources import _sync_notion

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()

        page = {
            "object": "page",
            "id": "db-row-1",
            "url": "https://notion.so/db-row-1",
            "created_time": "2026-01-01T00:00:00.000Z",
            "last_edited_time": "2026-06-01T00:00:00.000Z",
            "created_by": {"id": "user-1", "name": "Alice"},
            "parent": {"type": "database_id", "database_id": "db-1"},
            "properties": {
                "title": {"type": "title", "title": [{"plain_text": "Server Migration"}]},
                "Priority": {"type": "select", "select": {"name": "Critical"}},
                "Budget": {"type": "number", "number": 15000},
            },
        }
        search_response = {"results": [page], "has_more": False, "next_cursor": None}
        body_response = {
            "results": [
                {
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {
                        "rich_text": [{"plain_text": "Migrate the legacy server to new hardware."}],
                    },
                },
            ],
            "has_more": False,
            "next_cursor": None,
        }

        async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, search_response)

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, body_response)

        class _ZeroVectorProvider:
            model_id = "test-stub"
            dimensions = 768

            async def embed(
                self, texts: list[str], *, input_type: str = "search_document"
            ) -> list[list[float]]:
                del input_type
                return [[0.0] * 768 for _ in texts]

        engine = create_async_engine(DATABASE_URL)
        sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                await session.execute(
                    sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Property Test')"),
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
                            return_value=_ZeroVectorProvider(),
                        ),
                        patch(
                            "raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False,
                        ),
                    ):
                        stats = await _sync_notion(
                            session=session, tenant_id=tenant_id, source_id=source_id,
                            config={"token": "secret-fake-token"}, query="*", limit=50,
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
                    # Regression: before this fix, neither of these custom
                    # properties would appear anywhere in the chunked text.
                    assert "Priority: Critical" in chunk_text
                    assert "Budget: 15000" in chunk_text
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
