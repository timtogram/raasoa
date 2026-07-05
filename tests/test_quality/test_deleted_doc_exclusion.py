"""E2E tests for F-031: conflict/duplicate detection must not flag a
document against a previously-DELETED document's tombstone row.

Before this fix, none of the exact-duplicate, chunk-overlap,
title-supersession, or semantic-contradiction queries in
raasoa.quality.conflicts / raasoa.quality.duplicate filtered out
documents with status='deleted' (or review_status in
quarantined/rejected/superseded). Re-uploading a document with the same
content as one that had previously been deleted therefore got flagged
as a duplicate/contradiction against its own tombstone, penalizing its
quality score for no reason.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from raasoa.config import settings
from raasoa.models.document import Document

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

DIM = 768


def _vec(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


async def _load_document(session: AsyncSession, doc_id: uuid.UUID) -> Document:
    doc_result = await session.execute(
        sql_text("SELECT * FROM documents WHERE id = :id"), {"id": doc_id},
    )
    doc_row = doc_result.mappings().first()
    assert doc_row is not None
    columns = set(Document.__table__.columns.keys())
    return Document(**{k: v for k, v in doc_row.items() if k in columns})


@pytest.fixture
async def deleted_doc_scenario() -> AsyncGenerator[
    tuple[dict[str, object], async_sessionmaker[AsyncSession]], None,
]:
    """A tombstoned (status='deleted', review_status='rejected') document
    that shares content_hash/chunk hashes/title/embedding-neighborhood
    with a brand-new document — plus one still-live document sharing the
    exact same signals, to prove the fix doesn't over-suppress real
    conflicts."""
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    shared_text = "identical boilerplate paragraph shared across docs for f031"
    shared_hash = hashlib.sha256(shared_text.encode()).digest()
    doc_hash = hashlib.sha256(b"whole-document-content-f031").digest()

    ids: dict[str, object] = {
        "tenant_id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "doc_deleted": uuid.uuid4(),
        "doc_live": uuid.uuid4(),
        "doc_new": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'F031Test')"),
            {"id": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'notion', 'Src', '{}'::jsonb)"
            ),
            {"id": ids["source_id"], "tid": ids["tenant_id"]},
        )

        # Tombstoned document: same content_hash, same chunk hash, same
        # title, same embedding axis as the new document — must be
        # excluded from every pass.
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, content_hash, "
                " status, review_status, version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, :soid, 'Shared Title Document', :chash, "
                " 'deleted', 'rejected', 1, 1, 0)"
            ),
            {
                "id": ids["doc_deleted"], "tid": ids["tenant_id"], "sid": ids["source_id"],
                "soid": "f031-deleted", "chash": doc_hash,
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO chunks "
                "(id, document_id, chunk_index, content_hash, chunk_text, "
                " token_count, embedding, tsv) "
                "VALUES (:id, :did, 0, :hash, :text, 6, :emb, "
                " to_tsvector('simple', :text))"
            ),
            {
                "id": uuid.uuid4(), "did": ids["doc_deleted"], "hash": shared_hash,
                "text": shared_text, "emb": str(_vec(0)),
            },
        )

        # A LIVE document with the exact same content/chunk signals but
        # a slightly different title (pass 3 explicitly excludes an
        # identical title as "the same document", so a near-identical
        # one — same 60% prefix — is used to prove supersession
        # detection still fires against a live document).
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, content_hash, "
                " status, review_status, version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, :soid, 'Shared Title Document v2', :chash, "
                " 'indexed', 'auto_published', 1, 1, 0)"
            ),
            {
                "id": ids["doc_live"], "tid": ids["tenant_id"], "sid": ids["source_id"],
                "soid": "f031-live", "chash": doc_hash,
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO chunks "
                "(id, document_id, chunk_index, content_hash, chunk_text, "
                " token_count, embedding, tsv) "
                "VALUES (:id, :did, 0, :hash, :text, 6, :emb, "
                " to_tsvector('simple', :text))"
            ),
            {
                "id": uuid.uuid4(), "did": ids["doc_live"], "hash": shared_hash,
                "text": shared_text, "emb": str(_vec(0)),
            },
        )

        # The new document being re-ingested (same content_hash as
        # both the tombstone and the live doc; distinct chunk hashes
        # are passed explicitly by each test).
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, content_hash, "
                " status, review_status, version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, :soid, 'Shared Title Document', :chash, "
                " 'indexed', 'auto_published', 1, 1, 0)"
            ),
            {
                "id": ids["doc_new"], "tid": ids["tenant_id"], "sid": ids["source_id"],
                "soid": "f031-new", "chash": doc_hash,
            },
        )
        await session.commit()

    yield ids, sessionmaker

    async with sessionmaker() as session:
        await session.execute(
            sql_text("DELETE FROM conflict_candidates WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM review_tasks WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.commit()
    await engine.dispose()


async def test_exact_duplicate_excludes_deleted_document(
    deleted_doc_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """Re-inserting a document with the same content_hash as a
    previously-deleted document must not be flagged as an exact
    duplicate against the tombstone — but must still be flagged against
    a live document with the same hash."""
    from raasoa.quality.conflicts import detect_conflicts

    ids, sessionmaker = deleted_doc_scenario

    async with sessionmaker() as session:
        doc = await _load_document(session, ids["doc_new"])  # type: ignore[arg-type]
        conflicts = await detect_conflicts(
            session, doc, ids["tenant_id"],  # type: ignore[arg-type]
            chunk_hashes=[],
            chunk_embeddings=[],
        )

    dup_conflicts = [c for c in conflicts if c.conflict_type == "exact_duplicate"]
    matched_doc_ids = {c.document_b_id for c in dup_conflicts}

    assert ids["doc_deleted"] not in matched_doc_ids
    assert ids["doc_live"] in matched_doc_ids


async def test_chunk_overlap_excludes_deleted_document(
    deleted_doc_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """A chunk-hash overlap against a deleted document's chunks must not
    be flagged as partial_overlap, but overlap against a live document
    still must be."""
    from raasoa.quality.conflicts import detect_conflicts

    ids, sessionmaker = deleted_doc_scenario
    shared_text = "identical boilerplate paragraph shared across docs for f031"
    shared_hash = hashlib.sha256(shared_text.encode()).digest()

    async with sessionmaker() as session:
        doc = await _load_document(session, ids["doc_new"])  # type: ignore[arg-type]
        conflicts = await detect_conflicts(
            session, doc, ids["tenant_id"],  # type: ignore[arg-type]
            chunk_hashes=[shared_hash, shared_hash],
            chunk_embeddings=[],
        )

    overlap_conflicts = [c for c in conflicts if c.conflict_type == "partial_overlap"]
    matched_doc_ids = {c.document_b_id for c in overlap_conflicts}

    assert ids["doc_deleted"] not in matched_doc_ids


async def test_title_supersession_excludes_deleted_document(
    deleted_doc_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """A similar-title match against a deleted document must not be
    flagged as potential_supersession, but a live document with a
    similar title still must be."""
    from raasoa.quality.conflicts import detect_conflicts

    ids, sessionmaker = deleted_doc_scenario

    async with sessionmaker() as session:
        doc = await _load_document(session, ids["doc_new"])  # type: ignore[arg-type]
        conflicts = await detect_conflicts(
            session, doc, ids["tenant_id"],  # type: ignore[arg-type]
            chunk_hashes=[],
            chunk_embeddings=[],
        )

    supersession_conflicts = [
        c for c in conflicts if c.conflict_type == "potential_supersession"
    ]
    matched_doc_ids = {c.document_b_id for c in supersession_conflicts}

    assert ids["doc_deleted"] not in matched_doc_ids
    assert ids["doc_live"] in matched_doc_ids


async def test_semantic_contradiction_excludes_deleted_document(
    deleted_doc_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """A semantically-close chunk (different content_hash) belonging to
    a deleted document must not be flagged potential_contradiction, but
    the same close-embedding chunk on a live document still must be."""
    from raasoa.quality.conflicts import detect_conflicts

    ids, sessionmaker = deleted_doc_scenario
    new_text = "a slightly different paragraph that embeds nearby for f031"
    new_hash = hashlib.sha256(new_text.encode()).digest()

    async with sessionmaker() as session:
        doc = await _load_document(session, ids["doc_new"])  # type: ignore[arg-type]
        conflicts = await detect_conflicts(
            session, doc, ids["tenant_id"],  # type: ignore[arg-type]
            chunk_hashes=[new_hash],
            chunk_embeddings=[_vec(0)],  # same axis as both other docs' chunk
        )

    contradiction_conflicts = [
        c for c in conflicts if c.conflict_type == "potential_contradiction"
    ]
    matched_doc_ids = {c.document_b_id for c in contradiction_conflicts}

    assert ids["doc_deleted"] not in matched_doc_ids
    assert ids["doc_live"] in matched_doc_ids


async def test_check_exact_duplicate_excludes_deleted_document(
    deleted_doc_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """raasoa.quality.duplicate.check_exact_duplicate must also skip
    tombstoned documents."""
    from raasoa.quality.duplicate import check_exact_duplicate

    ids, sessionmaker = deleted_doc_scenario
    doc_hash = hashlib.sha256(b"whole-document-content-f031").digest()

    async with sessionmaker() as session:
        match = await check_exact_duplicate(
            session, ids["tenant_id"], doc_hash,  # type: ignore[arg-type]
            exclude_doc_id=ids["doc_new"],  # type: ignore[arg-type]
        )

    assert match is not None
    assert match.document_id == ids["doc_live"]


async def test_check_chunk_overlap_excludes_deleted_document(
    deleted_doc_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """raasoa.quality.duplicate.check_chunk_overlap must also skip
    tombstoned documents."""
    from raasoa.quality.duplicate import check_chunk_overlap

    ids, sessionmaker = deleted_doc_scenario
    shared_text = "identical boilerplate paragraph shared across docs for f031"
    shared_hash = hashlib.sha256(shared_text.encode()).digest()

    async with sessionmaker() as session:
        matches = await check_chunk_overlap(
            session, ids["tenant_id"],  # type: ignore[arg-type]
            chunk_hashes=[shared_hash, shared_hash],
            exclude_doc_id=ids["doc_new"],  # type: ignore[arg-type]
        )

    matched_doc_ids = {m.document_id for m in matches}
    assert ids["doc_deleted"] not in matched_doc_ids
