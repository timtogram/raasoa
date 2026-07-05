"""E2E tests for deletion cascade (F-010 / F-025).

Before this fix, POST /v1/webhooks/ingest with event=document.deleted only
flipped documents.status to 'deleted' — it never touched chunks, claims,
acl_entries, or crm_objects for that document. A deleted HubSpot record's
owner ACL grant and CRM object row persisted forever, and orphaned
chunks/claims remained fully queryable by any code path that doesn't
filter on document status.

These tests prove that after document.deleted, the acl_entries,
crm_objects, chunks, and claims rows for that document are actually gone
— not just the document's own status flag flipped.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

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
    """See tests/test_api/test_documents_acl.py for why this is needed:
    raasoa.db.engine is a loop-bound singleton, pytest-asyncio's default
    loop is function-scoped, and disposing before/after each test avoids
    "attached to a different loop" errors from stale pooled connections.
    """
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def _client() -> AsyncClient:
    from raasoa.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def deletion_scenario() -> AsyncGenerator[dict[str, object], None]:
    """A document fully wired up with a chunk, a claim, an ACL grant, and
    a crm_objects row — everything the document.deleted webhook handler
    must clean up when the underlying record is deleted at the source.

    Uses the well-known DEFAULT_TENANT: with AUTH_ENABLED=false (the test
    default, see tests/conftest.py and test_ingestion.py's `tenant_id`
    fixture), resolve_tenant_async() in the webhook handler always
    resolves to DEFAULT_TENANT regardless of request headers, so the
    document under test must live under that same tenant to be found.
    """
    from raasoa.db import async_session
    from raasoa.middleware.auth import DEFAULT_TENANT

    tenant_id = DEFAULT_TENANT
    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    acl_id = uuid.uuid4()
    crm_id = uuid.uuid4()
    source_object_id = f"hubspot:deals:{uuid.uuid4().hex[:8]}"

    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM tenants WHERE id = :tid"), {"tid": tenant_id},
        )
        if not result.first():
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Default Tenant')"),
                {"id": tenant_id},
            )

        # The webhook handler looks up (or creates) a source keyed by
        # (tenant_id, source_type) — reuse whatever source already exists
        # for DEFAULT_TENANT + 'hubspot' rather than inserting a
        # colliding one, since the handler will resolve to that existing
        # row, not one this fixture might otherwise create separately.
        source_result = await session.execute(
            sql_text(
                "SELECT id FROM sources WHERE tenant_id = :tid AND source_type = 'hubspot'"
            ),
            {"tid": tenant_id},
        )
        source_row = source_result.first()
        if source_row:
            source_id = source_row.id
        else:
            source_id = uuid.uuid4()
            await session.execute(
                sql_text(
                    "INSERT INTO sources "
                    "(id, tenant_id, source_type, name, connection_config) "
                    "VALUES (:id, :tid, 'hubspot', 'CRM', '{}'::jsonb)"
                ),
                {"id": source_id, "tid": tenant_id},
            )
        await session.commit()

    async with async_session() as session:
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " review_status, version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, :soid, 'Deal To Delete', 'indexed', "
                " 'published', 1, 1, 0)"
            ),
            {"id": doc_id, "tid": tenant_id, "sid": source_id, "soid": source_object_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO chunks "
                "(id, document_id, chunk_index, content_hash, chunk_text) "
                "VALUES (:id, :did, 0, :hash, 'some chunk text')"
            ),
            {"id": chunk_id, "did": doc_id, "hash": b"deadbeef"},
        )
        await session.execute(
            sql_text(
                "INSERT INTO claims "
                "(id, tenant_id, document_id, chunk_id, subject, predicate, "
                " object_value, confidence, evidence_span) "
                "VALUES (:id, :tid, :did, :cid, 'Deal', 'has_amount', '9000', "
                " 0.9, 'the deal is worth 9000')"
            ),
            {"id": claim_id, "tid": tenant_id, "did": doc_id, "cid": chunk_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO acl_entries "
                "(id, document_id, principal_type, principal_id, permission, source_acl_id) "
                "VALUES (:id, :did, 'user', 'hubspot:owner:42', 'read', 'hubspot_owner:42')"
            ),
            {"id": acl_id, "did": doc_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO crm_objects "
                "(id, tenant_id, source_id, document_id, object_type, external_id, "
                " owner_principal_id, properties) "
                "VALUES (:id, :tid, :sid, :did, 'deals', 'ext-123', "
                " 'hubspot:owner:42', '{}'::jsonb)"
            ),
            {"id": crm_id, "tid": tenant_id, "sid": source_id, "did": doc_id},
        )
        await session.commit()

    yield {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "claim_id": claim_id,
        "acl_id": acl_id,
        "crm_id": crm_id,
        "source_object_id": source_object_id,
    }

    async with async_session() as session:
        await session.execute(
            sql_text("DELETE FROM crm_objects WHERE document_id = :did"), {"did": doc_id},
        )
        await session.execute(
            sql_text("DELETE FROM acl_entries WHERE document_id = :did"), {"did": doc_id},
        )
        await session.execute(
            sql_text("DELETE FROM claims WHERE document_id = :did"), {"did": doc_id},
        )
        await session.execute(
            sql_text("DELETE FROM chunks WHERE document_id = :did"), {"did": doc_id},
        )
        await session.execute(
            sql_text("DELETE FROM documents WHERE id = :did"), {"did": doc_id},
        )
        # Do NOT delete the shared DEFAULT_TENANT / hubspot source rows —
        # they're reused across tests (see the lookup-or-create above) and
        # deleting them here would break other tests running against the
        # same well-known tenant.
        await session.commit()


async def test_document_deleted_webhook_cascades_acl_crm_chunks_claims(
    deletion_scenario: dict[str, object],
) -> None:
    """F-010/F-025: after document.deleted, acl_entries, crm_objects,
    chunks, and claims for the document must be gone — not merely the
    document's own status flag flipped to 'deleted'."""
    from raasoa.db import async_session

    doc_id = deletion_scenario["doc_id"]
    source_object_id = deletion_scenario["source_object_id"]

    async with await _client() as client:
        resp = await client.post(
            "/v1/webhooks/ingest",
            json={
                "event": "document.deleted",
                "source": "hubspot",
                "source_object_id": source_object_id,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"

    async with async_session() as session:
        doc_row = (
            await session.execute(
                sql_text("SELECT status FROM documents WHERE id = :did"), {"did": doc_id},
            )
        ).first()
        assert doc_row is not None
        assert doc_row.status == "deleted"

        acl_row = (
            await session.execute(
                sql_text("SELECT 1 FROM acl_entries WHERE document_id = :did"), {"did": doc_id},
            )
        ).first()
        assert acl_row is None, "acl_entries row must be deleted, not orphaned"

        crm_row = (
            await session.execute(
                sql_text("SELECT 1 FROM crm_objects WHERE document_id = :did"), {"did": doc_id},
            )
        ).first()
        assert crm_row is None, "crm_objects row must be deleted, not orphaned"

        chunk_row = (
            await session.execute(
                sql_text("SELECT 1 FROM chunks WHERE document_id = :did"), {"did": doc_id},
            )
        ).first()
        assert chunk_row is None, "chunks row must be deleted, not orphaned"

        claim_row = (
            await session.execute(
                sql_text("SELECT 1 FROM claims WHERE document_id = :did"), {"did": doc_id},
            )
        ).first()
        assert claim_row is None, "claims row must be deleted, not orphaned"


async def test_document_deleted_webhook_is_idempotent_and_scoped(
    deletion_scenario: dict[str, object],
) -> None:
    """Deleting an unrelated source_object_id must not touch this
    document's data, and a repeat delete of the same object is a no-op
    (0 affected) rather than erroring."""
    from raasoa.db import async_session

    doc_id = deletion_scenario["doc_id"]

    async with await _client() as client:
        # Unrelated delete — must not cascade against our document.
        resp = await client.post(
            "/v1/webhooks/ingest",
            json={
                "event": "document.deleted",
                "source": "hubspot",
                "source_object_id": "hubspot:deals:does-not-exist",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Deletion processed (0 affected)"

    async with async_session() as session:
        chunk_row = (
            await session.execute(
                sql_text("SELECT 1 FROM chunks WHERE document_id = :did"), {"did": doc_id},
            )
        ).first()
        assert chunk_row is not None, "unrelated delete must not touch this document's chunks"


async def test_sharepoint_delta_delete_cascades_acl_crm_chunks_claims() -> None:
    """F-010/F-025: the SharePoint delta-sync delete-propagation path
    (_delete_sharepoint_item) has the identical gap as the webhook
    handler — it must also cascade-delete acl_entries, crm_objects,
    chunks, and claims for the document, not just flip its status."""
    from raasoa.api.sources import _delete_sharepoint_item, _sharepoint_source_object_id
    from raasoa.db import async_session

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    acl_id = uuid.uuid4()
    crm_id = uuid.uuid4()
    drive_id = "drive-abc"
    item_id = "item-123"
    source_object_id = _sharepoint_source_object_id(drive_id, item_id)

    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'SP Cascade Test')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'sharepoint', 'Docs', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " review_status, version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, :soid, 'SP Doc To Delete', 'indexed', "
                " 'published', 1, 1, 0)"
            ),
            {"id": doc_id, "tid": tenant_id, "sid": source_id, "soid": source_object_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO chunks "
                "(id, document_id, chunk_index, content_hash, chunk_text) "
                "VALUES (:id, :did, 0, :hash, 'sp chunk text')"
            ),
            {"id": chunk_id, "did": doc_id, "hash": b"feedface"},
        )
        await session.execute(
            sql_text(
                "INSERT INTO claims "
                "(id, tenant_id, document_id, chunk_id, subject, predicate, "
                " object_value, confidence, evidence_span) "
                "VALUES (:id, :tid, :did, :cid, 'Doc', 'mentions', 'thing', "
                " 0.8, 'the doc mentions a thing')"
            ),
            {"id": claim_id, "tid": tenant_id, "did": doc_id, "cid": chunk_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO acl_entries "
                "(id, document_id, principal_type, principal_id, permission, source_acl_id) "
                "VALUES (:id, :did, 'user', 'user:alice', 'read', 'sp-perm-1')"
            ),
            {"id": acl_id, "did": doc_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO crm_objects "
                "(id, tenant_id, source_id, document_id, object_type, external_id, "
                " owner_principal_id, properties) "
                "VALUES (:id, :tid, :sid, :did, 'files', 'sp-ext-1', "
                " NULL, '{}'::jsonb)"
            ),
            {"id": crm_id, "tid": tenant_id, "sid": source_id, "did": doc_id},
        )
        await session.commit()

    async with async_session() as session:
        deleted_count = await _delete_sharepoint_item(
            session=session,
            tenant_id=tenant_id,
            source_id=source_id,
            drive_id=drive_id,
            item_id=item_id,
        )
        assert deleted_count == 1

    async with async_session() as session:
        doc_row = (
            await session.execute(
                sql_text("SELECT status FROM documents WHERE id = :did"), {"did": doc_id},
            )
        ).first()
        assert doc_row is not None
        assert doc_row.status == "deleted"

        for table in ("acl_entries", "crm_objects", "chunks", "claims"):
            row = (
                await session.execute(
                    sql_text(f"SELECT 1 FROM {table} WHERE document_id = :did"),
                    {"did": doc_id},
                )
            ).first()
            assert row is None, f"{table} row must be deleted, not orphaned"

        await session.execute(
            sql_text("DELETE FROM documents WHERE id = :did"), {"did": doc_id},
        )
        await session.execute(
            sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id},
        )
        await session.execute(
            sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
        )
        await session.commit()
