"""Tests for SharePoint Lists discovery and ingestion (F-046 follow-up).

SharePoint Lists live under /sites/{id}/lists, a completely separate
Graph API surface from /drives. Nothing in this connector called it
before this feature -- structured internal data kept in Lists (trackers,
indexes, directories) was entirely undiscoverable, not merely unparsed.

Requires a live Postgres for the end-to-end tests. Skips gracefully when
unreachable.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from raasoa.api.sources import (
    _sharepoint_list_field_value_to_text,
    _sharepoint_list_item_title,
    _sharepoint_list_item_to_markdown,
)
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


class TestSharePointLists:
    """Pure unit test against _sharepoint_lists' filtering logic."""

    async def test_document_libraries_and_hidden_lists_are_excluded(self) -> None:
        from raasoa.api.sources import _sharepoint_lists

        response = {
            "value": [
                {"id": "list-1", "displayName": "Documents", "hidden": False,
                 "list": {"template": "documentLibrary"}},
                {"id": "list-2", "displayName": "Hidden System List", "hidden": True,
                 "list": {"template": "genericList"}},
                {"id": "list-3", "displayName": "Project Tracker", "hidden": False,
                 "list": {"template": "genericList"}},
            ],
        }

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, response)

        client = AsyncMock()
        client.get = _get

        lists = await _sharepoint_lists(client, {}, "site-1")

        assert len(lists) == 1
        assert lists[0]["displayName"] == "Project Tracker"

    async def test_pagination_follows_odata_next_link(self) -> None:
        from raasoa.api.sources import _sharepoint_lists

        page1 = {
            "value": [
                {"id": "list-a", "displayName": "List A", "hidden": False,
                 "list": {"template": "genericList"}},
            ],
            "@odata.nextLink": "https://graph/next-page",
        }
        page2 = {
            "value": [
                {"id": "list-b", "displayName": "List B", "hidden": False,
                 "list": {"template": "genericList"}},
            ],
        }

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, page2 if "next-page" in url else page1)

        client = AsyncMock()
        client.get = _get

        lists = await _sharepoint_lists(client, {}, "site-1")

        assert {lst["displayName"] for lst in lists} == {"List A", "List B"}


class TestSharePointListFieldValueToText:
    def test_scalar_values_render_directly(self) -> None:
        assert _sharepoint_list_field_value_to_text("hello") == "hello"
        assert _sharepoint_list_field_value_to_text(42) == "42"
        assert _sharepoint_list_field_value_to_text(3.5) == "3.5"

    def test_boolean_renders_as_yes_no(self) -> None:
        assert _sharepoint_list_field_value_to_text(True) == "Yes"
        assert _sharepoint_list_field_value_to_text(False) == "No"

    def test_none_renders_as_empty_string(self) -> None:
        assert _sharepoint_list_field_value_to_text(None) == ""

    def test_list_of_scalars_comma_joined(self) -> None:
        assert _sharepoint_list_field_value_to_text(["a", "b", "c"]) == "a, b, c"

    def test_lookup_dict_uses_lookup_value(self) -> None:
        assert (
            _sharepoint_list_field_value_to_text({"LookupId": 5, "LookupValue": "Widget"})
            == "Widget"
        )

    def test_person_dict_uses_title_or_display_name(self) -> None:
        assert _sharepoint_list_field_value_to_text({"Title": "Alice Smith"}) == "Alice Smith"
        assert (
            _sharepoint_list_field_value_to_text({"DisplayName": "Bob Jones"}) == "Bob Jones"
        )

    def test_dict_with_no_known_key_renders_empty(self) -> None:
        assert _sharepoint_list_field_value_to_text({"SomeInternalKey": "x"}) == ""

    def test_multi_value_lookup_list_of_dicts(self) -> None:
        value = [{"LookupValue": "Widget A"}, {"LookupValue": "Widget B"}]
        assert _sharepoint_list_field_value_to_text(value) == "Widget A, Widget B"


class TestSharePointListItemToMarkdown:
    def test_title_and_fields_rendered_excluding_system_fields(self) -> None:
        fields = {
            "Title": "Q3 Server Migration",
            "Priority": "High",
            "Budget": 15000,
            "Completed": False,
            "id": "42",
            "ContentType": "Item",
            "Modified": "2026-06-01T00:00:00Z",
            "AuthorLookupId": "7",
        }
        title, body = _sharepoint_list_item_to_markdown("Project Tracker", fields, "42")

        assert title == "Q3 Server Migration"
        assert "# Q3 Server Migration" in body
        assert "List: Project Tracker" in body
        assert "Priority: High" in body
        assert "Budget: 15000" in body
        assert "Completed: No" in body
        # System/internal fields must not leak into the rendered body.
        assert "ContentType" not in body
        assert "AuthorLookupId" not in body
        assert "Modified: 2026-06-01" not in body  # excluded, not just re-labeled

    def test_falls_back_to_item_id_when_no_title_field(self) -> None:
        title, body = _sharepoint_list_item_to_markdown("Tracker", {"Priority": "Low"}, "99")
        assert title == "Item 99"
        assert "# Item 99" in body

    def test_item_title_prefers_title_then_linktitle_then_name(self) -> None:
        assert _sharepoint_list_item_title({"Title": "A", "Name": "B"}, "1") == "A"
        assert _sharepoint_list_item_title({"LinkTitle": "C"}, "1") == "C"
        assert _sharepoint_list_item_title({"Name": "D"}, "1") == "D"
        assert _sharepoint_list_item_title({}, "5") == "Item 5"


@pytest.mark.skipif(not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}")
class TestSyncSharePointListItemsEndToEnd:
    async def _make_session(self) -> async_sessionmaker[AsyncSession]:
        engine = create_async_engine(DATABASE_URL)
        return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), engine

    async def test_list_items_are_discovered_and_ingested(self) -> None:
        from raasoa.api.sources import _sync_sharepoint_list_items

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()

        list_obj = {"id": "list-1", "displayName": "Project Tracker"}
        items_response = {
            "value": [
                {
                    "id": "item-1",
                    "webUrl": "https://sp.example.com/lists/tracker/1",
                    "lastModifiedDateTime": "2026-06-01T00:00:00Z",
                    "fields": {
                        "Title": "Migrate database",
                        "Priority": "High",
                        "AssignedTo": {"Title": "Alice"},
                    },
                },
            ],
            "@odata.nextLink": None,
        }

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, items_response)

        client = AsyncMock()
        client.get = _get

        sessionmaker, engine = await self._make_session()
        try:
            async with sessionmaker() as session:
                await session.execute(
                    sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'List Sync Test')"),
                    {"id": tenant_id},
                )
                await session.execute(
                    sql_text(
                        "INSERT INTO sources "
                        "(id, tenant_id, source_type, name, connection_config) "
                        "VALUES (:id, :tid, 'sharepoint', 'Docs', '{}'::jsonb)"
                    ),
                    {"id": source_id, "tid": tenant_id},
                )
                await session.commit()

                try:
                    stats: dict[str, Any] = {"found": 0, "synced": 0, "skipped": 0, "errors": []}
                    with patch(
                        "raasoa.providers.factory.get_embedding_provider",
                        return_value=_zero_vector_provider(),
                    ):
                        active_ids, complete = await _sync_sharepoint_list_items(
                            session=session, tenant_id=tenant_id, source_id=source_id,
                            client=client, headers={}, site_id="site-1",
                            list_obj=list_obj, limit=50, stats=stats,
                        )

                    assert complete is True
                    assert active_ids == {"sharepoint:list:list-1:item-1"}
                    assert stats["synced"] == 1

                    doc = (
                        await session.execute(
                            sql_text(
                                "SELECT title FROM documents "
                                "WHERE tenant_id = :tid AND source_id = :sid"
                            ),
                            {"tid": tenant_id, "sid": source_id},
                        )
                    ).first()
                    assert doc is not None
                    assert doc.title == "Migrate database"
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

    async def test_limit_truncation_marks_incomplete(self) -> None:
        from raasoa.api.sources import _sync_sharepoint_list_items

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()

        list_obj = {"id": "list-1", "displayName": "Big Tracker"}
        items_response = {
            "value": [
                {
                    "id": f"item-{i}",
                    "webUrl": f"https://sp.example.com/lists/tracker/{i}",
                    "lastModifiedDateTime": "2026-06-01T00:00:00Z",
                    "fields": {"Title": f"Task {i}", "Priority": "Medium"},
                }
                for i in range(5)
            ],
        }

        async def _get(url: str, **_kw: Any) -> _FakeResponse:
            return _FakeResponse(200, items_response)

        client = AsyncMock()
        client.get = _get

        sessionmaker, engine = await self._make_session()
        try:
            async with sessionmaker() as session:
                await session.execute(
                    sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'List Limit Test')"),
                    {"id": tenant_id},
                )
                await session.execute(
                    sql_text(
                        "INSERT INTO sources "
                        "(id, tenant_id, source_type, name, connection_config) "
                        "VALUES (:id, :tid, 'sharepoint', 'Docs', '{}'::jsonb)"
                    ),
                    {"id": source_id, "tid": tenant_id},
                )
                await session.commit()

                try:
                    stats: dict[str, Any] = {"found": 0, "synced": 0, "skipped": 0, "errors": []}
                    with patch(
                        "raasoa.providers.factory.get_embedding_provider",
                        return_value=_zero_vector_provider(),
                    ):
                        active_ids, complete = await _sync_sharepoint_list_items(
                            session=session, tenant_id=tenant_id, source_id=source_id,
                            client=client, headers={}, site_id="site-1",
                            list_obj=list_obj, limit=2, stats=stats,
                        )

                    assert complete is False
                    assert stats["synced"] == 2
                    assert len(active_ids) == 2
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


@pytest.mark.skipif(not _db_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}")
class TestMarkSharePointListItemsDeleted:
    async def test_item_missing_from_active_set_is_soft_deleted(self) -> None:
        from raasoa.api.sources import _mark_sharepoint_list_items_deleted

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()
        doc_id = uuid.uuid4()

        engine = create_async_engine(DATABASE_URL)
        sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                await session.execute(
                    sql_text(
                        "INSERT INTO tenants (id, name) VALUES (:id, 'List Delete Test')"
                    ),
                    {"id": tenant_id},
                )
                await session.execute(
                    sql_text(
                        "INSERT INTO sources "
                        "(id, tenant_id, source_type, name, connection_config) "
                        "VALUES (:id, :tid, 'sharepoint', 'Docs', '{}'::jsonb)"
                    ),
                    {"id": source_id, "tid": tenant_id},
                )
                await session.execute(
                    sql_text(
                        "INSERT INTO documents "
                        "(id, tenant_id, source_id, source_object_id, title, status, "
                        " review_status, version, chunk_count, access_count) "
                        "VALUES (:id, :tid, :sid, 'sharepoint:list:list-1:item-gone', "
                        " 'Gone Item', 'indexed', 'published', 1, 0, 0)"
                    ),
                    {"id": doc_id, "tid": tenant_id, "sid": source_id},
                )
                await session.commit()

                try:
                    count = await _mark_sharepoint_list_items_deleted(
                        session, tenant_id, source_id,
                        active_object_ids={"sharepoint:list:list-1:item-alive"},
                    )
                    assert count == 1

                    row = (
                        await session.execute(
                            sql_text("SELECT status FROM documents WHERE id = :id"),
                            {"id": doc_id},
                        )
                    ).first()
                    assert row is not None
                    assert row.status == "deleted"
                finally:
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

    async def test_empty_active_set_never_mass_deletes(self) -> None:
        from raasoa.api.sources import _mark_sharepoint_list_items_deleted

        tenant_id = uuid.uuid4()
        source_id = uuid.uuid4()
        doc_id = uuid.uuid4()

        engine = create_async_engine(DATABASE_URL)
        sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                await session.execute(
                    sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Empty Guard Test')"),
                    {"id": tenant_id},
                )
                await session.execute(
                    sql_text(
                        "INSERT INTO sources "
                        "(id, tenant_id, source_type, name, connection_config) "
                        "VALUES (:id, :tid, 'sharepoint', 'Docs', '{}'::jsonb)"
                    ),
                    {"id": source_id, "tid": tenant_id},
                )
                await session.execute(
                    sql_text(
                        "INSERT INTO documents "
                        "(id, tenant_id, source_id, source_object_id, title, status, "
                        " review_status, version, chunk_count, access_count) "
                        "VALUES (:id, :tid, :sid, 'sharepoint:list:list-1:item-x', "
                        " 'Item X', 'indexed', 'published', 1, 0, 0)"
                    ),
                    {"id": doc_id, "tid": tenant_id, "sid": source_id},
                )
                await session.commit()

                try:
                    count = await _mark_sharepoint_list_items_deleted(
                        session, tenant_id, source_id, active_object_ids=set(),
                    )
                    assert count == 0

                    row = (
                        await session.execute(
                            sql_text("SELECT status FROM documents WHERE id = :id"),
                            {"id": doc_id},
                        )
                    ).first()
                    assert row is not None
                    assert row.status == "indexed"
                finally:
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
