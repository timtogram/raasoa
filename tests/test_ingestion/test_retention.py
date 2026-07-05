"""Tests for the retention/GDPR hard-delete purge job (F-010/F-025).

Before this fix, run_retention_cleanup's per-table purge list did not
include acl_entries or crm_objects — a tenant retention-driven hard delete
of a document would remove chunks/claims/findings/feedback and the
document row itself, but leave that document's ACL grants and CRM object
row orphaned in the database forever (neither table has a foreign key to
documents, so nothing else cleans them up).

This test proves acl_entries and crm_objects rows for a purged document
are gone after run_retention_cleanup(), and the stats dict accounts for
them.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

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


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    """run_retention_cleanup() uses raasoa.db.async_session, whose
    underlying raasoa.db.engine is a loop-bound singleton. pytest-asyncio's
    default event loop is function-scoped (a fresh loop per test), so a
    pooled connection opened on one test's loop can be checked out again
    during a later test on a different/already-closed loop, raising
    "attached to a different loop" / "Event loop is closed" errors that
    have nothing to do with retention logic itself. Disposing before AND
    after each test forces fresh, loop-correct connections. See
    tests/test_api/test_documents_acl.py for the same pattern.
    """
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def private_sessionmaker() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()


async def test_retention_cleanup_purges_acl_entries_and_crm_objects(
    private_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """F-010/F-025: a hard-delete-eligible, expired soft-deleted document's
    acl_entries and crm_objects rows must be purged along with its chunks
    and claims — not left behind as orphans."""
    from raasoa.worker.retention import run_retention_cleanup

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    acl_id = uuid.uuid4()
    crm_id = uuid.uuid4()

    async with private_sessionmaker() as session:
        await session.execute(
            sql_text(
                "INSERT INTO tenants "
                "(id, name, retention_days, hard_delete_enabled) "
                "VALUES (:id, 'Retention Cascade Test', 1, true)"
            ),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'hubspot', 'CRM', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        # created_at far enough in the past to be older than
        # retention_days=1, and status='deleted' so it's purge-eligible.
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " review_status, version, chunk_count, access_count, created_at) "
                "VALUES (:id, :tid, :sid, 'hubspot:deals:purge-me', 'Purge Candidate', "
                " 'deleted', 'rejected', 1, 1, 0, now() - interval '30 days')"
            ),
            {"id": doc_id, "tid": tenant_id, "sid": source_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO chunks "
                "(id, document_id, chunk_index, content_hash, chunk_text) "
                "VALUES (:id, :did, 0, :hash, 'chunk text')"
            ),
            {"id": chunk_id, "did": doc_id, "hash": b"cafebabe"},
        )
        await session.execute(
            sql_text(
                "INSERT INTO claims "
                "(id, tenant_id, document_id, chunk_id, subject, predicate, "
                " object_value, confidence, evidence_span) "
                "VALUES (:id, :tid, :did, :cid, 'Deal', 'has_amount', '5000', "
                " 0.9, 'the deal is worth 5000')"
            ),
            {"id": claim_id, "tid": tenant_id, "did": doc_id, "cid": chunk_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO acl_entries "
                "(id, document_id, principal_type, principal_id, permission, source_acl_id) "
                "VALUES (:id, :did, 'user', 'hubspot:owner:99', 'read', 'hubspot_owner:99')"
            ),
            {"id": acl_id, "did": doc_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO crm_objects "
                "(id, tenant_id, source_id, document_id, object_type, external_id, "
                " owner_principal_id, properties) "
                "VALUES (:id, :tid, :sid, :did, 'deals', 'purge-ext-1', "
                " 'hubspot:owner:99', '{}'::jsonb)"
            ),
            {"id": crm_id, "tid": tenant_id, "sid": source_id, "did": doc_id},
        )
        await session.commit()

    try:
        stats = await run_retention_cleanup()

        assert stats["documents_purged"] >= 1
        assert stats["chunks_purged"] >= 1
        assert stats["claims_purged"] >= 1
        assert stats["acl_entries_purged"] >= 1
        assert stats["crm_objects_purged"] >= 1

        async with private_sessionmaker() as session:
            doc_row = (
                await session.execute(
                    sql_text("SELECT 1 FROM documents WHERE id = :did"), {"did": doc_id},
                )
            ).first()
            assert doc_row is None, "document row must be hard-deleted"

            acl_row = (
                await session.execute(
                    sql_text("SELECT 1 FROM acl_entries WHERE document_id = :did"),
                    {"did": doc_id},
                )
            ).first()
            assert acl_row is None, "acl_entries row must be purged, not orphaned"

            crm_row = (
                await session.execute(
                    sql_text("SELECT 1 FROM crm_objects WHERE document_id = :did"),
                    {"did": doc_id},
                )
            ).first()
            assert crm_row is None, "crm_objects row must be purged, not orphaned"

            chunk_row = (
                await session.execute(
                    sql_text("SELECT 1 FROM chunks WHERE document_id = :did"), {"did": doc_id},
                )
            ).first()
            assert chunk_row is None

            claim_row = (
                await session.execute(
                    sql_text("SELECT 1 FROM claims WHERE document_id = :did"), {"did": doc_id},
                )
            ).first()
            assert claim_row is None
    finally:
        async with private_sessionmaker() as session:
            # Best-effort cleanup in case an assertion failed before purge
            # completed (document may already be gone if it succeeded).
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
            await session.execute(
                sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
            )
            await session.commit()


async def test_retention_cleanup_ignores_tenants_without_hard_delete_enabled(
    private_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A soft-deleted, expired document under a tenant WITHOUT
    hard_delete_enabled must be left alone entirely — proving the purge
    job's tenant gate still works after adding the new tables to its
    cascade list."""
    from raasoa.worker.retention import run_retention_cleanup

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    doc_id = uuid.uuid4()

    async with private_sessionmaker() as session:
        await session.execute(
            sql_text(
                "INSERT INTO tenants "
                "(id, name, retention_days, hard_delete_enabled) "
                "VALUES (:id, 'No Hard Delete Test', 1, false)"
            ),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'hubspot', 'CRM', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " review_status, version, chunk_count, access_count, created_at) "
                "VALUES (:id, :tid, :sid, 'hubspot:deals:keep-me', 'Keep Candidate', "
                " 'deleted', 'rejected', 1, 0, 0, now() - interval '30 days')"
            ),
            {"id": doc_id, "tid": tenant_id, "sid": source_id},
        )
        await session.commit()

    try:
        await run_retention_cleanup()

        async with private_sessionmaker() as session:
            doc_row = (
                await session.execute(
                    sql_text("SELECT 1 FROM documents WHERE id = :did"), {"did": doc_id},
                )
            ).first()
            assert doc_row is not None, (
                "document under a tenant without hard_delete_enabled must not be purged"
            )
    finally:
        async with private_sessionmaker() as session:
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
