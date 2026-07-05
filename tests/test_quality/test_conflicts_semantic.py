"""E2E test for the semantic-contradiction pass's duplicate-hash fix
(F-030).

Before this fix, a chunk with an IDENTICAL content_hash (i.e. identical
text) to a chunk in another document was still scored at
distance≈0 → confidence≈1.0 "potential_contradiction" — every partial
re-upload or shared boilerplate paragraph flagged itself as
contradicting its own earlier copy, contrary to the pass's own
docstring ("close embeddings but DIFFERENT content_hashes").

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


async def _load_document(session: AsyncSession, doc_id: uuid.UUID) -> Document:
    doc_result = await session.execute(
        sql_text("SELECT * FROM documents WHERE id = :id"), {"id": doc_id},
    )
    doc_row = doc_result.mappings().first()
    assert doc_row is not None
    columns = set(Document.__table__.columns.keys())
    return Document(**{k: v for k, v in doc_row.items() if k in columns})


def _vec(axis: int) -> list[float]:
    """A unit vector along a single axis — orthogonal vectors (distance
    axes differing) give pgvector cosine distance ~1.0 (well outside the
    default 0.15 threshold), while the same axis gives distance 0.0
    (well inside it). This keeps "close" vs "far" unambiguous instead of
    relying on a small perturbation that both fall inside threshold of."""
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


@pytest.fixture
async def scenario() -> AsyncGenerator[
    tuple[dict[str, object], async_sessionmaker[AsyncSession]], None,
]:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids: dict[str, object] = {
        "tenant_id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "doc_other_dup": uuid.uuid4(),
        "doc_other_diff": uuid.uuid4(),
        "doc_new": uuid.uuid4(),
    }
    duplicate_text = "identical boilerplate paragraph shared across docs"
    different_text = "a genuinely different but topically-close paragraph"
    dup_hash = hashlib.sha256(duplicate_text.encode()).digest()
    diff_hash = hashlib.sha256(different_text.encode()).digest()

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'SemanticConflictTest')"),
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
        for doc_id, title, text_val, chash, embed_axis in [
            (ids["doc_other_dup"], "Other Doc (duplicate text)", duplicate_text, dup_hash, 0),
            (ids["doc_other_diff"], "Other Doc (different text)", different_text, diff_hash, 1),
        ]:
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " version, chunk_count, access_count) "
                    "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', 1, 1, 0)"
                ),
                {
                    "id": doc_id, "tid": ids["tenant_id"], "sid": ids["source_id"],
                    "soid": f"sc-{doc_id.hex[:6]}", "title": title,
                },
            )
            await session.execute(
                sql_text(
                    "INSERT INTO chunks "
                    "(id, document_id, chunk_index, content_hash, chunk_text, "
                    " token_count, embedding, tsv) "
                    "VALUES (:id, :did, 0, :hash, :text, 5, :emb, "
                    " to_tsvector('simple', :text))"
                ),
                {
                    "id": uuid.uuid4(), "did": doc_id, "hash": chash, "text": text_val,
                    "emb": str(_vec(embed_axis)),
                },
            )
        # The new document itself (pass 4 excludes same-document chunks).
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, :soid, 'New Doc', 'indexed', 1, 1, 0)"
            ),
            {
                "id": ids["doc_new"], "tid": ids["tenant_id"], "sid": ids["source_id"],
                "soid": f"sc-{ids['doc_new'].hex[:6]}",
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


async def test_identical_content_hash_is_not_a_contradiction(
    scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """A chunk with the exact same content_hash as another document's
    chunk must never be flagged 'potential_contradiction' — it's the
    exact-duplicate case, not a contradiction."""
    from raasoa.quality.conflicts import detect_conflicts

    ids, sessionmaker = scenario
    duplicate_text = "identical boilerplate paragraph shared across docs"
    dup_hash = hashlib.sha256(duplicate_text.encode()).digest()

    async with sessionmaker() as session:
        doc = await _load_document(session, ids["doc_new"])  # type: ignore[arg-type]

        conflicts = await detect_conflicts(
            session, doc, ids["tenant_id"],  # type: ignore[arg-type]
            chunk_hashes=[dup_hash],
            chunk_embeddings=[_vec(0)],  # same axis as doc_other_dup — close match
        )

    semantic_conflicts = [c for c in conflicts if c.conflict_type == "potential_contradiction"]
    assert semantic_conflicts == []


async def test_different_content_hash_close_embedding_is_still_flagged(
    scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """Sanity check: the fix doesn't over-correct — a genuinely
    different chunk that embeds close to an existing one must still be
    flagged as a potential contradiction."""
    from raasoa.quality.conflicts import detect_conflicts

    ids, sessionmaker = scenario
    new_text = "a slightly different paragraph that embeds nearby"
    new_hash = hashlib.sha256(new_text.encode()).digest()

    async with sessionmaker() as session:
        doc = await _load_document(session, ids["doc_new"])  # type: ignore[arg-type]

        conflicts = await detect_conflicts(
            session, doc, ids["tenant_id"],  # type: ignore[arg-type]
            chunk_hashes=[new_hash],
            chunk_embeddings=[_vec(1)],  # same axis as doc_other_diff — close match
        )

    semantic_conflicts = [c for c in conflicts if c.conflict_type == "potential_contradiction"]
    assert len(semantic_conflicts) == 1
    assert semantic_conflicts[0].document_b_id == ids["doc_other_diff"]
