"""E2E test: retrieval feedback boost is capped well below RRF scale
(F-012).

Before this fix, the feedback boost (COALESCE(SUM(rating),0)/COUNT(*) *
0.1) could reach ±0.1 — roughly 3x the maximum achievable combined RRF
score (~0.033) — with no query-similarity filter, so a single positive
rating on a chunk could outrank a genuinely more relevant chunk for any
unrelated query, tenant-wide.

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


class _ZeroVectorProvider:
    """Embeddings play no role here — ranking is driven by lexical match
    and the feedback boost, which is exactly what's under test."""

    model_id = "test-stub"
    dimensions = 768

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        return [[0.0] * 768 for _ in texts]


async def _add_doc_with_chunk(
    session: AsyncSession, doc_id: uuid.UUID, chunk_id: uuid.UUID,
    source_id: uuid.UUID, tenant_id: uuid.UUID, title: str, text_val: str,
) -> None:
    await session.execute(
        sql_text(
            "INSERT INTO documents "
            "(id, tenant_id, source_id, source_object_id, title, status, "
            " version, chunk_count, access_count) "
            "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', 1, 1, 0)"
        ),
        {
            "id": doc_id, "tid": tenant_id, "sid": source_id,
            "soid": f"fb-{doc_id.hex[:6]}", "title": title,
        },
    )
    await session.execute(
        sql_text(
            "INSERT INTO chunks "
            "(id, document_id, chunk_index, content_hash, chunk_text, "
            " token_count, embedding, tsv) "
            "VALUES (:id, :did, 0, :hash, :text, 5, :emb, to_tsvector('simple', :text))"
        ),
        {
            "id": chunk_id, "did": doc_id,
            "hash": hashlib.sha256(text_val.encode()).digest(),
            "text": text_val, "emb": str([0.0] * 768),
        },
    )


@pytest.fixture
async def scenario() -> AsyncGenerator[
    tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]], None,
]:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids = {
        "tenant_id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "doc_relevant": uuid.uuid4(),
        "chunk_relevant": uuid.uuid4(),
        "doc_irrelevant": uuid.uuid4(),
        "chunk_irrelevant": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'FeedbackBoostTest')"),
            {"id": ids["tenant_id"]},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config, default_visibility) "
                "VALUES (:id, :tid, 'notion', 'Src', '{}'::jsonb, 'inherit')"
            ),
            {"id": ids["source_id"], "tid": ids["tenant_id"]},
        )
        await _add_doc_with_chunk(
            session, ids["doc_relevant"], ids["chunk_relevant"], ids["source_id"],
            ids["tenant_id"], "Widget Pricing Policy",
            "our widget pricing policy sets the base price at 50 dollars",
        )
        await _add_doc_with_chunk(
            session, ids["doc_irrelevant"], ids["chunk_irrelevant"], ids["source_id"],
            ids["tenant_id"], "Cat Grooming Schedule",
            "the cat grooming schedule is every six weeks at the salon",
        )
        # A single maximum-positive rating on the IRRELEVANT chunk, from
        # an unrelated original query — proving the boost has no
        # similarity filter yet still must not flip a clear outcome.
        await session.execute(
            sql_text(
                "INSERT INTO retrieval_feedback "
                "(id, tenant_id, query_text, chunk_id, document_id, rating) "
                "VALUES (:id, :tid, 'totally unrelated query', :cid, :did, 1.0)"
            ),
            {
                "id": uuid.uuid4(), "tid": ids["tenant_id"],
                "cid": ids["chunk_irrelevant"], "did": ids["doc_irrelevant"],
            },
        )
        await session.commit()

    yield ids, sessionmaker

    async with sessionmaker() as session:
        await session.execute(
            sql_text("DELETE FROM retrieval_feedback WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
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


async def test_max_rating_does_not_outrank_a_relevant_chunk(
    scenario: tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]],
) -> None:
    """The exact escalation from F-012: even a maximum +1.0 rating on an
    unrelated chunk must not outrank a genuinely lexically-relevant
    chunk for a query the rating was never associated with."""
    from raasoa.retrieval.hybrid_search import search

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        results = await search(
            session=session,
            query="widget pricing policy",
            tenant_id=ids["tenant_id"],
            embedding_provider=_ZeroVectorProvider(),
            top_k=10,
        )

    assert len(results) >= 1
    assert results[0].document_id == ids["doc_relevant"], (
        "the boosted-but-irrelevant chunk outranked the relevant one"
    )


async def test_boost_still_nudges_near_ties(
    scenario: tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]],
) -> None:
    """The fix is a cap, not a removal: for a query matching BOTH chunks
    equally well lexically, the rated chunk should still edge out the
    unrated one — proving the boost isn't just zeroed out."""
    from raasoa.retrieval.hybrid_search import hybrid_search

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        # A shared word ("schedule") only in the irrelevant/rated chunk
        # would break the tie unfairly; instead compare the two chunks'
        # raw rrf_score directly via hybrid_search with no query match
        # at all, isolating the feedback boost as the only signal.
        results = await hybrid_search(
            session=session,
            query="zzz_no_lexical_match_zzz",
            query_embedding=[0.0] * 768,
            tenant_id=ids["tenant_id"],
            top_k=10,
        )
    by_doc = {r.document_id: r.score for r in results}
    assert ids["doc_irrelevant"] in by_doc
    assert ids["doc_relevant"] in by_doc
    # Both chunks tie on lexical (neither matches) and nearly tie on
    # semantic (identical zero-vector embeddings — Postgres still
    # assigns an arbitrary-but-deterministic ROW_NUMBER between them,
    # contributing a small ordering delta of its own). The rated
    # chunk's score must be strictly higher, but nowhere near the old
    # uncapped ±0.1 boost — this margin is generous enough to absorb
    # that tie-order noise while still proving the boost is real.
    boost_delta = by_doc[ids["doc_irrelevant"]] - by_doc[ids["doc_relevant"]]
    assert 0 < boost_delta < 0.01
