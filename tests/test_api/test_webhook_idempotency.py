"""Tests for webhook idempotency-key dedup (F-046).

``WebhookPayload.idempotency_key`` was documented as "prevents duplicate
processing on retries" but was never actually read anywhere -- a retried
webhook delivery (e.g. after a network timeout where the caller never saw
the first response) was fully reprocessed every time. This is mostly
harmless for document.created/updated (ingest_file already dedupes on
content hash), but document.deleted and the data-contract-rejection path
had no dedup of their own at all.

These tests prove: a retried delivery with the same idempotency_key
returns the cached response without reprocessing; a delivery with no key
is unaffected; a transient failure is NOT cached (must remain retryable);
and the 48h TTL purge in worker.retention actually removes old rows.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sql_text

from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

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


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


class _ZeroVectorProvider:
    model_id = "test-stub"
    dimensions = 768

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        return [[0.0] * 768 for _ in texts]


async def _client() -> AsyncClient:
    from raasoa.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def tenant_and_source() -> AsyncGenerator[dict[str, object], None]:
    from raasoa.db import async_session
    from raasoa.middleware.auth import DEFAULT_TENANT

    tenant_id = DEFAULT_TENANT
    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM tenants WHERE id = :tid"), {"tid": tenant_id},
        )
        if not result.first():
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Default Tenant')"),
                {"id": tenant_id},
            )
            await session.commit()

    before_docs = await _document_ids(tenant_id)

    yield {"tenant_id": tenant_id}

    after_docs = await _document_ids(tenant_id)
    new_doc_ids = list(after_docs - before_docs)
    async with async_session() as session:
        if new_doc_ids:
            await session.execute(
                sql_text("DELETE FROM chunks WHERE document_id = ANY(:ids)"),
                {"ids": new_doc_ids},
            )
            await session.execute(
                sql_text("DELETE FROM documents WHERE id = ANY(:ids)"),
                {"ids": new_doc_ids},
            )
        await session.execute(
            sql_text(
                "DELETE FROM webhook_idempotency_keys WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        await session.commit()


async def _document_ids(tenant_id: uuid.UUID) -> set[uuid.UUID]:
    from raasoa.db import async_session

    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM documents WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        return {row.id for row in result.fetchall()}


class TestIdempotentCreate:
    async def test_same_key_twice_creates_only_one_document(
        self, tenant_and_source: dict[str, object],
    ) -> None:
        key = f"idem-{uuid.uuid4().hex[:8]}"
        source_object_id = f"webhook-item-{uuid.uuid4().hex[:8]}"

        with (
            patch(
                "raasoa.api.webhooks.get_embedding_provider",
                return_value=_ZeroVectorProvider(),
            ),
            patch(
                "raasoa.ingestion.pipeline.settings.claim_extraction_enabled",
                False,
            ),
        ):
            async with await _client() as client:
                payload = {
                    "event": "document.created",
                    "source": "custom",
                    "source_object_id": source_object_id,
                    "content": "Some webhook-delivered content for idempotency testing.",
                    "idempotency_key": key,
                }
                resp1 = await client.post("/v1/webhooks/ingest", json=payload)
                resp2 = await client.post("/v1/webhooks/ingest", json=payload)

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        body1, body2 = resp1.json(), resp2.json()
        assert body1["document_id"] == body2["document_id"]
        assert body1 == body2

        from raasoa.db import async_session

        async with async_session() as session:
            count = (
                await session.execute(
                    sql_text(
                        "SELECT count(*) AS n FROM documents "
                        "WHERE source_object_id = :soid"
                    ),
                    {"soid": source_object_id},
                )
            ).first()
            assert count is not None
            assert count.n == 1

    async def test_no_key_still_works_unaffected(
        self, tenant_and_source: dict[str, object],
    ) -> None:
        """No regression when idempotency_key is absent (None)."""
        source_object_id = f"webhook-item-{uuid.uuid4().hex[:8]}"

        with (
            patch(
                "raasoa.api.webhooks.get_embedding_provider",
                return_value=_ZeroVectorProvider(),
            ),
            patch(
                "raasoa.ingestion.pipeline.settings.claim_extraction_enabled",
                False,
            ),
        ):
            async with await _client() as client:
                resp = await client.post(
                    "/v1/webhooks/ingest",
                    json={
                        "event": "document.created",
                        "source": "custom",
                        "source_object_id": source_object_id,
                        "content": "Content with no idempotency key at all, over 50 chars.",
                    },
                )

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"


class TestIdempotentDelete:
    async def test_retried_delete_returns_cached_response_without_reprocessing(
        self, tenant_and_source: dict[str, object],
    ) -> None:
        key = f"idem-del-{uuid.uuid4().hex[:8]}"
        source_object_id = f"webhook-del-{uuid.uuid4().hex[:8]}"

        with (
            patch(
                "raasoa.api.webhooks.get_embedding_provider",
                return_value=_ZeroVectorProvider(),
            ),
            patch(
                "raasoa.ingestion.pipeline.settings.claim_extraction_enabled",
                False,
            ),
        ):
            async with await _client() as client:
                await client.post(
                    "/v1/webhooks/ingest",
                    json={
                        "event": "document.created",
                        "source": "custom",
                        "source_object_id": source_object_id,
                        "content": "Doc that will be deleted twice via webhook, over 50 chars.",
                    },
                )

        with patch(
            "raasoa.api.sources._cascade_delete_document_data",
        ) as mock_cascade:
            async with await _client() as client:
                resp1 = await client.post(
                    "/v1/webhooks/ingest",
                    json={
                        "event": "document.deleted",
                        "source": "custom",
                        "source_object_id": source_object_id,
                        "idempotency_key": key,
                    },
                )
                resp2 = await client.post(
                    "/v1/webhooks/ingest",
                    json={
                        "event": "document.deleted",
                        "source": "custom",
                        "source_object_id": source_object_id,
                        "idempotency_key": key,
                    },
                )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()
        # The cascade delete helper must only run once -- the second
        # delivery is a cache hit that short-circuits before it.
        assert mock_cascade.call_count == 1


class TestTransientFailureNotCached:
    async def test_failed_delivery_is_retryable_with_same_key(
        self, tenant_and_source: dict[str, object],
    ) -> None:
        key = f"idem-fail-{uuid.uuid4().hex[:8]}"
        source_object_id = f"webhook-fail-{uuid.uuid4().hex[:8]}"
        payload = {
            "event": "document.created",
            "source": "custom",
            "source_object_id": source_object_id,
            "content": "Content for a call that will fail the first time, long enough.",
            "idempotency_key": key,
        }

        with patch(
            "raasoa.api.webhooks.ingest_file",
            side_effect=RuntimeError("simulated transient failure"),
        ):
            async with await _client() as client:
                failing_resp = await client.post("/v1/webhooks/ingest", json=payload)
        assert failing_resp.status_code == 500

        # Retry with the SAME key, now without the simulated failure --
        # must actually succeed, not return a cached failure.
        with (
            patch(
                "raasoa.api.webhooks.get_embedding_provider",
                return_value=_ZeroVectorProvider(),
            ),
            patch(
                "raasoa.ingestion.pipeline.settings.claim_extraction_enabled",
                False,
            ),
        ):
            async with await _client() as client:
                retry_resp = await client.post("/v1/webhooks/ingest", json=payload)

        assert retry_resp.status_code == 200
        assert retry_resp.json()["status"] == "processed"


class TestIdempotencyKeyPurge:
    async def test_run_retention_cleanup_purges_expired_keys(
        self, tenant_and_source: dict[str, object],
    ) -> None:
        from raasoa.db import async_session
        from raasoa.worker.retention import run_retention_cleanup

        tenant_id = tenant_and_source["tenant_id"]

        async with async_session() as session:
            await session.execute(
                sql_text(
                    "INSERT INTO webhook_idempotency_keys "
                    "(tenant_id, idempotency_key, response_json, created_at) "
                    "VALUES (:tid, :key, '{}'::jsonb, now() - interval '72 hours')"
                ),
                {"tid": tenant_id, "key": f"stale-{uuid.uuid4().hex[:8]}"},
            )
            await session.commit()

        stats = await run_retention_cleanup()
        assert stats["idempotency_keys_purged"] >= 1

        async with async_session() as session:
            remaining = (
                await session.execute(
                    sql_text(
                        "SELECT count(*) AS n FROM webhook_idempotency_keys "
                        "WHERE created_at < now() - interval '48 hours'"
                    )
                )
            ).first()
            assert remaining is not None
            assert remaining.n == 0
