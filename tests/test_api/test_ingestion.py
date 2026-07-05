"""Tests for F-026: POST /v1/ingest must enforce max_file_size_mb WHILE
reading the upload, not after buffering the entire body into memory.

Previously ``file_data = await file.read()`` read the whole upload before
the size check ran, letting an attacker exhaust server memory with an
oversized body before the 413 was ever produced. The fix reads in bounded
1MB chunks and raises 413 as soon as the accumulated size exceeds the
configured limit, without draining the rest of the body.

Requires a live Postgres. Skips gracefully when unreachable, matching the
pattern used by the other tests in this directory.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sql_text
from starlette.datastructures import UploadFile

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
    """Minimal EmbeddingProvider stub -- no network, no real embeddings.

    768 dims to match the pgvector column width used by the chunks table
    (see raasoa.config.Settings.embedding_dimensions).
    """

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
async def tenant_id() -> AsyncGenerator[uuid.UUID, None]:
    """With AUTH_ENABLED=false (the test default, see tests/conftest.py),
    resolve_tenant_async() always resolves to the well-known DEFAULT_TENANT
    regardless of request headers, so tests must ingest against that id.
    Documents created for it here are cleaned up afterwards so this test
    doesn't leak rows/skew quota counts for other tests reusing it."""
    from raasoa.db import async_session
    from raasoa.middleware.auth import DEFAULT_TENANT

    tid = DEFAULT_TENANT
    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM tenants WHERE id = :tid"), {"tid": tid},
        )
        if not result.first():
            await session.execute(
                sql_text(
                    "INSERT INTO tenants (id, name) VALUES (:id, 'Default Tenant')"
                ),
                {"id": tid},
            )
            await session.commit()

    before = await _document_ids(tid)

    yield tid

    after = await _document_ids(tid)
    new_doc_ids = list(after - before)
    if new_doc_ids:
        async with async_session() as session:
            await session.execute(
                sql_text("DELETE FROM chunks WHERE document_id = ANY(:ids)"),
                {"ids": new_doc_ids},
            )
            await session.execute(
                sql_text("DELETE FROM documents WHERE id = ANY(:ids)"),
                {"ids": new_doc_ids},
            )
            await session.commit()


async def _document_ids(tid: uuid.UUID) -> set[uuid.UUID]:
    from raasoa.db import async_session

    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM documents WHERE tenant_id = :tid"), {"tid": tid},
        )
        return {row.id for row in result.fetchall()}


def _tenant_header(tid: uuid.UUID) -> dict[str, str]:
    del tid  # kept for call-site clarity; tenant is fixed via DEFAULT_TENANT
    return {}


class TestIngestSizeLimitEnforcedDuringRead:
    async def test_oversized_upload_rejected_without_buffering_whole_body(
        self, tenant_id: uuid.UUID,
    ) -> None:
        """A body far larger than max_file_size_mb must be rejected with
        413, and the handler must not have read anywhere near the full
        oversized body -- proving the check happens during, not after,
        the read loop."""
        original_max = settings.max_file_size_mb
        settings.max_file_size_mb = 2  # 2MB limit for a fast test
        chunk_size = 1024 * 1024

        # 6x the limit: if the old bug were present, all ~6 chunks would
        # be read before any check ran. With the fix, the loop must stop
        # after the 3rd chunk at the latest (2MB limit / 1MB chunks + 1
        # chunk to cross the threshold).
        oversized_body = b"A" * (chunk_size * 6)

        read_calls: list[int] = []
        original_read = UploadFile.read

        async def _counting_read(self: UploadFile, size: int = -1) -> bytes:
            data = await original_read(self, size)
            read_calls.append(len(data))
            return data

        try:
            with (
                patch.object(UploadFile, "read", _counting_read),
                patch(
                    "raasoa.api.ingestion.get_embedding_provider",
                    return_value=_ZeroVectorProvider(),
                ),
                patch(
                    "raasoa.ingestion.pipeline.settings.claim_extraction_enabled",
                    False,
                ),
            ):
                async with await _client() as client:
                    resp = await client.post(
                        "/v1/ingest",
                        headers=_tenant_header(tenant_id),
                        files={
                            "file": (
                                "oversized.txt",
                                oversized_body,
                                "text/plain",
                            )
                        },
                    )
        finally:
            settings.max_file_size_mb = original_max

        assert resp.status_code == 413
        assert "too large" in resp.json()["detail"].lower()

        # Non-empty chunks actually handed to the handler's read loop.
        non_empty_reads = [n for n in read_calls if n > 0]
        total_read = sum(non_empty_reads)

        # The handler must stop well before consuming the whole 6MB body:
        # it should bail out at (or just past) the 2MB limit, i.e. after
        # at most 3 chunk reads (2 full 1MB chunks + 1 that crosses the
        # threshold), never draining all 6 chunks.
        assert len(non_empty_reads) <= 3, (
            f"expected early termination within ~3 chunk reads, "
            f"got {len(non_empty_reads)} reads: {non_empty_reads}"
        )
        assert total_read < len(oversized_body), (
            "handler read the entire oversized body before rejecting it"
        )

    async def test_upload_under_limit_still_ingests_successfully(
        self, tenant_id: uuid.UUID,
    ) -> None:
        """Uploads under the configured limit must be unaffected: same
        content ends up ingested, same 200 response shape as before."""
        original_max = settings.max_file_size_mb
        settings.max_file_size_mb = 5
        content = b"The quick brown fox jumps over the lazy dog. " * 50

        try:
            with (
                patch(
                    "raasoa.api.ingestion.get_embedding_provider",
                    return_value=_ZeroVectorProvider(),
                ),
                patch(
                    "raasoa.ingestion.pipeline.settings.claim_extraction_enabled",
                    False,
                ),
            ):
                async with await _client() as client:
                    resp = await client.post(
                        "/v1/ingest",
                        headers=_tenant_header(tenant_id),
                        files={
                            "file": (
                                "small.txt",
                                content,
                                "text/plain",
                            )
                        },
                    )
        finally:
            settings.max_file_size_mb = original_max

        assert resp.status_code == 200
        body = resp.json()
        assert body["chunk_count"] >= 1
        assert body["title"]

    async def test_empty_upload_still_rejected_with_400(
        self, tenant_id: uuid.UUID,
    ) -> None:
        """The pre-existing empty-file 400 behavior must be unchanged."""
        with (
            patch(
                "raasoa.api.ingestion.get_embedding_provider",
                return_value=_ZeroVectorProvider(),
            ),
        ):
            async with await _client() as client:
                resp = await client.post(
                    "/v1/ingest",
                    headers=_tenant_header(tenant_id),
                    files={"file": ("empty.txt", b"", "text/plain")},
                )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Empty file"
