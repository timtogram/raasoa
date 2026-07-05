"""E2E test: knowledge_index excludes claims from restricted sources.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid

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


async def test_build_index_excludes_restricted_source_claims() -> None:
    """A restricted-source claim must never enter the knowledge index —
    this fast-lookup layer has no per-query principal awareness, so the
    only safe policy is excluding restricted facts at build time."""
    from raasoa.retrieval.knowledge_index import build_index, lookup

    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid.uuid4()
    src_open = uuid.uuid4()
    src_restricted = uuid.uuid4()
    doc_open = uuid.uuid4()
    doc_restricted = uuid.uuid4()

    try:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'IdxACLTest')"),
                {"id": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources "
                    "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                    "VALUES (:id, :tid, 'notion', 'Open', '{}'::jsonb, 'inherit')"
                ),
                {"id": src_open, "tid": tenant_id},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO sources "
                    "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                    "VALUES (:id, :tid, 'hubspot', 'Restricted', '{}'::jsonb, 'restricted')"
                ),
                {"id": src_restricted, "tid": tenant_id},
            )
            for doc_id, sid, title in [
                (doc_open, src_open, "Open Doc"),
                (doc_restricted, src_restricted, "Restricted Doc"),
            ]:
                await session.execute(
                    sql_text(
                        "INSERT INTO documents "
                        "(id, tenant_id, source_id, source_object_id, title, status, "
                        " review_status, version, chunk_count, access_count) "
                        "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', "
                        " 'published', 1, 1, 0)"
                    ),
                    {
                        "id": doc_id, "tid": tenant_id, "sid": sid,
                        "soid": f"i-{doc_id.hex[:6]}", "title": title,
                    },
                )
            await session.execute(
                sql_text(
                    "INSERT INTO claims "
                    "(id, tenant_id, document_id, subject, predicate, object_value, "
                    " confidence, evidence_span, status) "
                    "VALUES (:id, :tid, :did, 'Acme', 'open fact predicate', "
                    " 'public value', 0.9, 'evidence', 'active')"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "did": doc_open},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO claims "
                    "(id, tenant_id, document_id, subject, predicate, object_value, "
                    " confidence, evidence_span, status) "
                    "VALUES (:id, :tid, :did, 'Acme', 'restricted deal predicate', "
                    " 'secret value', 0.9, 'evidence', 'active')"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "did": doc_restricted},
            )
            await session.commit()

            stats = await build_index(session, tenant_id)
            assert stats["entries"] == 1

            open_result = await lookup(session, tenant_id, "open fact predicate")
            assert open_result.found

            restricted_result = await lookup(session, tenant_id, "restricted deal predicate")
            assert not restricted_result.found
    finally:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM knowledge_index WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM claims WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
            )
            await session.commit()
        await engine.dispose()


async def test_build_index_excludes_document_level_acl_claims() -> None:
    """Regression for F-009: a document on a NON-restricted source that
    has its own acl_entries grant must still be excluded from the
    knowledge index. Per acl_predicate_sql's semantics, a document with
    an ACL entry is visible only to a matching principal regardless of
    the source's default_visibility — the index-build filter previously
    only checked source.default_visibility, so such a document's claims
    were answerable via the index layer to any tenant caller."""
    from raasoa.retrieval.knowledge_index import build_index, lookup

    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    doc_open = uuid.uuid4()
    doc_acl_protected = uuid.uuid4()

    try:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'IdxDocAclTest')"),
                {"id": tenant_id},
            )
            # Both documents share the SAME non-restricted source.
            await session.execute(
                sql_text(
                    "INSERT INTO sources "
                    "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                    "VALUES (:id, :tid, 'notion', 'Shared Source', '{}'::jsonb, 'inherit')"
                ),
                {"id": source_id, "tid": tenant_id},
            )
            for doc_id, title in [
                (doc_open, "Open Doc"),
                (doc_acl_protected, "ACL-Protected Doc"),
            ]:
                await session.execute(
                    sql_text(
                        "INSERT INTO documents "
                        "(id, tenant_id, source_id, source_object_id, title, status, "
                        " review_status, version, chunk_count, access_count) "
                        "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', "
                        " 'published', 1, 1, 0)"
                    ),
                    {
                        "id": doc_id, "tid": tenant_id, "sid": source_id,
                        "soid": f"i-{doc_id.hex[:6]}", "title": title,
                    },
                )
            # Grant only user:alice access to the ACL-protected doc.
            await session.execute(
                sql_text(
                    "INSERT INTO acl_entries "
                    "(id, document_id, principal_type, principal_id, permission) "
                    "VALUES (:id, :did, 'user', 'user:alice', 'read')"
                ),
                {"id": uuid.uuid4(), "did": doc_acl_protected},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO claims "
                    "(id, tenant_id, document_id, subject, predicate, object_value, "
                    " confidence, evidence_span, status) "
                    "VALUES (:id, :tid, :did, 'Acme', 'open fact predicate', "
                    " 'public value', 0.9, 'evidence', 'active')"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "did": doc_open},
            )
            await session.execute(
                sql_text(
                    "INSERT INTO claims "
                    "(id, tenant_id, document_id, subject, predicate, object_value, "
                    " confidence, evidence_span, status) "
                    "VALUES (:id, :tid, :did, 'Acme', 'acl protected predicate', "
                    " 'secret value', 0.9, 'evidence', 'active')"
                ),
                {"id": uuid.uuid4(), "tid": tenant_id, "did": doc_acl_protected},
            )
            await session.commit()

            stats = await build_index(session, tenant_id)
            assert stats["entries"] == 1

            open_result = await lookup(session, tenant_id, "open fact predicate")
            assert open_result.found

            protected_result = await lookup(session, tenant_id, "acl protected predicate")
            assert not protected_result.found
    finally:
        async with sessionmaker() as session:
            await session.execute(
                sql_text("DELETE FROM knowledge_index WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text(
                    "DELETE FROM acl_entries WHERE document_id IN (:d1, :d2)"
                ), {"d1": doc_open, "d2": doc_acl_protected},
            )
            await session.execute(
                sql_text("DELETE FROM claims WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": tenant_id},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
            )
            await session.commit()
        await engine.dispose()
