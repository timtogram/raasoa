"""E2E tests for F-032: incremental knowledge-index updates.

Before this fix, every document ingest triggered a full tenant-wide
knowledge_index rebuild (DELETE FROM knowledge_index WHERE tenant_id =
:tid, then re-insert everything from scratch). Two concurrent ingests
for the same tenant could interleave delete/insert and produce
duplicated or missing index entries, and the cost of a single-document
ingest scaled with the *total* number of claims in the tenant.

update_index_for_document() instead recomputes only the (subject,
predicate) groups that the new document's claims could have affected,
leaving every other tenant/document's existing entries untouched.

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


async def _insert_source(
    session: AsyncSession, source_id: uuid.UUID, tenant_id: uuid.UUID,
) -> None:
    await session.execute(
        sql_text(
            "INSERT INTO sources "
            "(id, tenant_id, source_type, name, connection_config, default_visibility) "
            "VALUES (:id, :tid, 'notion', 'Src', '{}'::jsonb, 'inherit')"
        ),
        {"id": source_id, "tid": tenant_id},
    )


async def _insert_document(
    session: AsyncSession, doc_id: uuid.UUID, tenant_id: uuid.UUID,
    source_id: uuid.UUID, soid: str, title: str,
) -> None:
    await session.execute(
        sql_text(
            "INSERT INTO documents "
            "(id, tenant_id, source_id, source_object_id, title, status, "
            " review_status, version, chunk_count, access_count) "
            "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', "
            " 'auto_published', 1, 1, 0)"
        ),
        {"id": doc_id, "tid": tenant_id, "sid": source_id, "soid": soid, "title": title},
    )


async def _insert_claim(
    session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID,
    subject: str, predicate: str, object_value: str, confidence: float,
) -> uuid.UUID:
    claim_id = uuid.uuid4()
    await session.execute(
        sql_text(
            "INSERT INTO claims "
            "(id, tenant_id, document_id, subject, predicate, object_value, "
            " confidence, evidence_span, status) "
            "VALUES (:id, :tid, :did, :subj, :pred, :val, :conf, 'evidence', 'active')"
        ),
        {
            "id": claim_id, "tid": tenant_id, "did": document_id,
            "subj": subject, "pred": predicate, "val": object_value, "conf": confidence,
        },
    )
    return claim_id


@pytest.fixture
async def tenant_scenario() -> AsyncGenerator[
    tuple[dict[str, object], async_sessionmaker[AsyncSession]], None,
]:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids: dict[str, object] = {
        "tenant_a": uuid.uuid4(),
        "tenant_b": uuid.uuid4(),
        "source_a": uuid.uuid4(),
        "source_b": uuid.uuid4(),
        "doc_a1": uuid.uuid4(),
        "doc_a2": uuid.uuid4(),
        "doc_b1": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        for tid, name in [
            (ids["tenant_a"], "F032TenantA"),
            (ids["tenant_b"], "F032TenantB"),
        ]:
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                {"id": tid, "name": name},
            )
        await _insert_source(session, ids["source_a"], ids["tenant_a"])  # type: ignore[arg-type]
        await _insert_source(session, ids["source_b"], ids["tenant_b"])  # type: ignore[arg-type]

        await _insert_document(
            session, ids["doc_a1"], ids["tenant_a"], ids["source_a"],  # type: ignore[arg-type]
            "a1", "Doc A1",
        )
        await _insert_document(
            session, ids["doc_a2"], ids["tenant_a"], ids["source_a"],  # type: ignore[arg-type]
            "a2", "Doc A2",
        )
        await _insert_document(
            session, ids["doc_b1"], ids["tenant_b"], ids["source_b"],  # type: ignore[arg-type]
            "b1", "Doc B1",
        )
        await session.commit()

    yield ids, sessionmaker

    async with sessionmaker() as session:
        for tid in [ids["tenant_a"], ids["tenant_b"]]:
            await session.execute(
                sql_text("DELETE FROM knowledge_index WHERE tenant_id = :tid"), {"tid": tid},
            )
            await session.execute(
                sql_text("DELETE FROM claims WHERE tenant_id = :tid"), {"tid": tid},
            )
            await session.execute(
                sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": tid},
            )
            await session.execute(
                sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": tid},
            )
            await session.execute(
                sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid},
            )
        await session.commit()
    await engine.dispose()


async def test_incremental_update_adds_new_document_claims(
    tenant_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """A brand-new document's claims must produce new knowledge_index
    entries via update_index_for_document, without needing a full
    build_index() rebuild."""
    from raasoa.retrieval.knowledge_index import lookup, update_index_for_document

    ids, sessionmaker = tenant_scenario

    async with sessionmaker() as session:
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
            "Acme Corp", "founding year", "1998", 0.9,
        )
        await session.commit()

        stats = await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
        )
        assert stats["entries_updated"] == 1

        result = await lookup(session, ids["tenant_a"], "founding year")  # type: ignore[arg-type]
        assert result.found
        assert result.entries[0].value == "1998"


async def test_incremental_update_does_not_wipe_other_documents_entries(
    tenant_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """Incrementally indexing doc_a2 must not remove the existing
    knowledge_index entry produced by doc_a1's unrelated claim."""
    from raasoa.retrieval.knowledge_index import lookup, update_index_for_document

    ids, sessionmaker = tenant_scenario

    async with sessionmaker() as session:
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
            "Acme Corp", "founding year", "1998", 0.9,
        )
        await session.commit()
        await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
        )

        # Now ingest a second, unrelated document/claim for the same tenant.
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
            "Acme Corp", "headquarters city", "Berlin", 0.9,
        )
        await session.commit()
        stats = await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
        )
        assert stats["entries_updated"] == 1

        # doc_a1's entry must still be present.
        founding = await lookup(session, ids["tenant_a"], "founding year")  # type: ignore[arg-type]
        assert founding.found
        assert founding.entries[0].value == "1998"

        # doc_a2's new entry must be present too.
        hq = await lookup(session, ids["tenant_a"], "headquarters city")  # type: ignore[arg-type]
        assert hq.found
        assert hq.entries[0].value == "Berlin"


async def test_incremental_update_does_not_touch_other_tenants(
    tenant_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """update_index_for_document for tenant A must never affect tenant
    B's knowledge_index rows (no tenant-wide DELETE)."""
    from raasoa.retrieval.knowledge_index import lookup, update_index_for_document

    ids, sessionmaker = tenant_scenario

    async with sessionmaker() as session:
        await _insert_claim(
            session, ids["tenant_b"], ids["doc_b1"],  # type: ignore[arg-type]
            "Globex Inc", "founding year", "2005", 0.9,
        )
        await session.commit()
        await update_index_for_document(
            session, ids["tenant_b"], ids["doc_b1"],  # type: ignore[arg-type]
        )

        # Now ingest into tenant A.
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
            "Acme Corp", "founding year", "1998", 0.9,
        )
        await session.commit()
        await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
        )

        # Tenant B's entry must be untouched.
        b_result = await lookup(session, ids["tenant_b"], "founding year")  # type: ignore[arg-type]
        assert b_result.found
        assert b_result.entries[0].value == "2005"


async def test_incremental_update_keeps_higher_confidence_existing_claim(
    tenant_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """When the new document's claim for a (subject, predicate) ties or
    loses against an existing higher/equal-confidence claim from
    another document, the index must keep the winning value — not
    blindly overwrite it with the newly-ingested document's claim."""
    from raasoa.retrieval.knowledge_index import lookup, update_index_for_document

    ids, sessionmaker = tenant_scenario

    async with sessionmaker() as session:
        # doc_a1 has the high-confidence, correct claim.
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
            "Acme Corp", "ceo name", "Jane Doe", 0.95,
        )
        await session.commit()
        await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
        )

        # doc_a2 is ingested later with a LOWER-confidence, conflicting claim
        # for the exact same (subject, predicate).
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
            "Acme Corp", "ceo name", "John Smith", 0.4,
        )
        await session.commit()
        stats = await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
        )
        # Still exactly one entry for this key (recomputed, not appended).
        assert stats["entries_updated"] == 1

        result = await lookup(session, ids["tenant_a"], "ceo name")  # type: ignore[arg-type]
        assert result.found
        assert len(result.entries) == 1
        # The higher-confidence claim (doc_a1's) must still win.
        assert result.entries[0].value == "Jane Doe"
        assert result.entries[0].confidence == pytest.approx(0.95)


async def test_incremental_update_ties_keep_first_by_confidence_order(
    tenant_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """On an exact confidence tie between the new document's claim and
    an existing claim for the same (subject, predicate), the grouping
    logic must deterministically pick one winner (mirroring
    build_index's own tie-break: DB ORDER BY confidence DESC, so ties
    fall back to whichever row Postgres returns first) rather than
    duplicating entries."""
    from raasoa.retrieval.knowledge_index import lookup, update_index_for_document

    ids, sessionmaker = tenant_scenario

    async with sessionmaker() as session:
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
            "Acme Corp", "stock ticker", "ACM", 0.8,
        )
        await session.commit()
        await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
        )

        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
            "Acme Corp", "stock ticker", "ACME", 0.8,
        )
        await session.commit()
        stats = await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
        )
        assert stats["entries_updated"] == 1

        result = await lookup(session, ids["tenant_a"], "stock ticker")  # type: ignore[arg-type]
        assert result.found
        assert len(result.entries) == 1
        assert result.entries[0].value in {"ACM", "ACME"}
        assert result.entries[0].confidence == pytest.approx(0.8)


async def test_incremental_update_matches_build_index_full_rebuild(
    tenant_scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
) -> None:
    """Sanity check that the incremental path and the full build_index()
    path converge on the same final state for the same claim set."""
    from raasoa.retrieval.knowledge_index import (
        build_index,
        lookup,
        update_index_for_document,
    )

    ids, sessionmaker = tenant_scenario

    async with sessionmaker() as session:
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
            "Acme Corp", "founding year", "1998", 0.9,
        )
        await _insert_claim(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
            "Acme Corp", "headquarters city", "Berlin", 0.85,
        )
        await session.commit()

        await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a1"],  # type: ignore[arg-type]
        )
        await update_index_for_document(
            session, ids["tenant_a"], ids["doc_a2"],  # type: ignore[arg-type]
        )

        incremental_founding = await lookup(
            session, ids["tenant_a"], "founding year",  # type: ignore[arg-type]
        )
        incremental_hq = await lookup(
            session, ids["tenant_a"], "headquarters city",  # type: ignore[arg-type]
        )

        # Now do a full rebuild and confirm identical results.
        await build_index(session, ids["tenant_a"])  # type: ignore[arg-type]

        rebuilt_founding = await lookup(
            session, ids["tenant_a"], "founding year",  # type: ignore[arg-type]
        )
        rebuilt_hq = await lookup(
            session, ids["tenant_a"], "headquarters city",  # type: ignore[arg-type]
        )

        assert incremental_founding.found and rebuilt_founding.found
        assert incremental_founding.entries[0].value == rebuilt_founding.entries[0].value

        assert incremental_hq.found and rebuilt_hq.found
        assert incremental_hq.entries[0].value == rebuilt_hq.entries[0].value
