"""End-to-end test for the HubSpot connector's sync logic.

No real HubSpot account is available in this environment, so the HubSpot
CRM Search API is mocked at the httpx.AsyncClient.post level — everything
downstream (ingestion, doc_metadata, owner-based ACL grant, delta cursor)
runs for real against a live Postgres.

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
    handle HubSpot API calls, not embedding calls too.

    Deliberately returns a DIFFERENT vector per call (seeded from a
    monotonic counter) rather than an all-zero vector for every text.
    Identical embeddings across records make conflict-detection treat
    every pair as a perfect semantic match, which — via an unrelated,
    pre-existing lazy-load hazard in the ingestion pipeline when a real
    conflict is flagged — crashes with a SQLAlchemy MissingGreenlet error
    that has nothing to do with HubSpot sync correctness. Distinct vectors
    keep this test representative of real (non-colliding) CRM records.
    """

    model_id = "test-stub"
    dimensions = 768

    def __init__(self) -> None:
        self._counter = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
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


DEALS_PAGE_1 = {
    "results": [
        {
            "id": "1001",
            "properties": {
                "dealname": "Acme Renewal",
                "amount": "50000",
                "dealstage": "closedwon",
                "hubspot_owner_id": "42",
                "hs_lastmodifieddate": "2026-06-01T00:00:00.000Z",
            },
        },
        {
            "id": "1002",
            "properties": {
                "dealname": "Beta Expansion",
                "amount": "12000",
                "dealstage": "appointmentscheduled",
                "hubspot_owner_id": None,
                "hs_lastmodifieddate": "2026-06-02T00:00:00.000Z",
            },
        },
    ],
    "paging": {},
}


async def _make_post_mock() -> AsyncMock:
    """A single AsyncMock reused for every object type's search call.

    Returns the deals page once per object type, then an empty page to
    stop pagination — matching how _sync_hubspot loops per object type.
    """
    seen_types: set[str] = set()

    async def _post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _FakeResponse:
        object_type = url.split("/crm/v3/objects/")[1].split("/search")[0]
        if object_type not in seen_types:
            seen_types.add(object_type)
            if object_type == "deals":
                return _FakeResponse(200, DEALS_PAGE_1)
            return _FakeResponse(200, {"results": [], "paging": {}})
        return _FakeResponse(200, {"results": [], "paging": {}})

    mock = AsyncMock(side_effect=_post)
    return mock


@pytest.fixture
async def private_sessionmaker() -> AsyncSession:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()


async def test_hubspot_sync_ingests_deals_with_owner_acl(
    private_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from raasoa.api.sources import _sync_hubspot

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()

    async with private_sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'HubSpot Test')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'hubspot', 'CRM', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        await session.commit()

        post_mock = await _make_post_mock()
        try:
            with (
                patch("httpx.AsyncClient.post", post_mock),
                patch(
                    "raasoa.providers.factory.get_embedding_provider",
                    return_value=_DistinctVectorProvider(),
                ),
                # Claim extraction is a separate, fire-and-forget subsystem
                # that also calls httpx (LLM chat) — unrelated to what this
                # test verifies (HubSpot sync correctness), and it isn't
                # concurrency-safe to share one AsyncSession with a
                # background task. Disable it for this test.
                patch("raasoa.ingestion.pipeline.settings.claim_extraction_enabled", False),
            ):
                stats = await _sync_hubspot(
                    session=session,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    config={"token": "pat-fake-token", "objects": ["deals"]},
                    query="*",
                    limit=50,
                )

            assert stats["synced"] == 2
            assert stats["by_object_type"]["deals"] == 2

            docs = await session.execute(
                sql_text(
                    "SELECT id, title, doc_metadata FROM documents "
                    "WHERE tenant_id = :tid AND source_id = :sid ORDER BY title"
                ),
                {"tid": tenant_id, "sid": source_id},
            )
            doc_rows = docs.fetchall()
            assert len(doc_rows) == 2
            titles = {r.title for r in doc_rows}
            assert titles == {"Acme Renewal", "Beta Expansion"}

            acme = next(r for r in doc_rows if r.title == "Acme Renewal")
            beta = next(r for r in doc_rows if r.title == "Beta Expansion")
            assert acme.doc_metadata["crm_object_type"] == "deals"
            assert acme.doc_metadata["amount"] == "50000"

            # Owner-based ACL: Acme has an owner -> gets a grant; Beta has none.
            acl_result = await session.execute(
                sql_text(
                    "SELECT principal_id, permission FROM acl_entries "
                    "WHERE document_id = :did"
                ),
                {"did": acme.id},
            )
            acl_rows = acl_result.fetchall()
            assert len(acl_rows) == 1
            assert acl_rows[0].principal_id == "hubspot:owner:42"
            assert acl_rows[0].permission == "read"

            no_acl_result = await session.execute(
                sql_text("SELECT 1 FROM acl_entries WHERE document_id = :did"),
                {"did": beta.id},
            )
            assert no_acl_result.first() is None

            # Each record is also upserted into crm_objects for the
            # structured query path (Task #15), with the same owner
            # attribution as the document-level ACL grant above.
            crm_result = await session.execute(
                sql_text(
                    "SELECT external_id, owner_principal_id, properties, document_id "
                    "FROM crm_objects WHERE tenant_id = :tid AND source_id = :sid "
                    "ORDER BY external_id"
                ),
                {"tid": tenant_id, "sid": source_id},
            )
            crm_rows = crm_result.fetchall()
            assert len(crm_rows) == 2
            assert crm_rows[0].external_id == "1001"
            assert crm_rows[0].owner_principal_id == "hubspot:owner:42"
            assert crm_rows[0].properties["dealname"] == "Acme Renewal"
            assert crm_rows[0].document_id == acme.id
            assert crm_rows[1].external_id == "1002"
            assert crm_rows[1].owner_principal_id is None

            # Delta cursor recorded for the next sync.
            cursor_result = await session.execute(
                sql_text(
                    "SELECT delta_token FROM sync_cursors "
                    "WHERE source_id = :sid AND source_type = 'hubspot'"
                ),
                {"sid": source_id},
            )
            cursor_row = cursor_result.first()
            assert cursor_row is not None
            assert "deals" in cursor_row.delta_token
        finally:
            await session.execute(
                sql_text("DELETE FROM crm_objects WHERE source_id = :sid"), {"sid": source_id},
            )
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


async def test_hubspot_sync_requires_token() -> None:
    from raasoa.api.sources import _sync_hubspot

    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with sessionmaker() as session:
            stats = await _sync_hubspot(
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


async def test_hubspot_sync_rejects_unknown_object_types() -> None:
    from raasoa.api.sources import _sync_hubspot

    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with sessionmaker() as session:
            stats = await _sync_hubspot(
                session=session,
                tenant_id=uuid.uuid4(),
                source_id=uuid.uuid4(),
                config={"token": "pat-fake", "objects": ["not_a_real_object"]},
                query="*",
                limit=50,
            )
        assert stats["status"] == "error"
    finally:
        await engine.dispose()
