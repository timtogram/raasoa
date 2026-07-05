"""E2E tests for claim-based contradiction detection (F-013).

Before this fix, detect_claim_conflicts compared only predicate
similarity and value difference — never subject — so "IT dept response
time = 4h" and "HR dept response time = 24h" would falsely flag as a
contradiction despite describing different entities entirely.

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


class _SamePredicateEmbeddingProvider:
    """Every predicate embeds identically — forces predicate similarity
    to always be 1.0 (>= the 0.7 threshold), isolating the subject
    comparison as the only thing that can prevent a false match."""

    model_id = "test-stub"
    dimensions = 8

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        return [[1.0] * 8 for _ in texts]


@pytest.fixture
async def scenario() -> AsyncGenerator[
    tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]], None,
]:
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids = {
        "tenant_id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "doc_existing": uuid.uuid4(),
        "doc_new": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'ClaimConflictsTest')"),
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
        for doc_id, title in [
            (ids["doc_existing"], "HR Policy"),
            (ids["doc_new"], "IT Policy"),
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
                    "soid": f"cc-{doc_id.hex[:6]}", "title": title,
                },
            )
        # An existing, unrelated-subject claim.
        await session.execute(
            sql_text(
                "INSERT INTO claims "
                "(id, tenant_id, document_id, subject, predicate, object_value, "
                " confidence, evidence_span, status) "
                "VALUES (:id, :tid, :did, 'HR dept', 'response time', '24h', "
                " 0.9, 'evidence', 'active')"
            ),
            {"id": uuid.uuid4(), "tid": ids["tenant_id"], "did": ids["doc_existing"]},
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
            sql_text("DELETE FROM claims WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
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


async def test_different_subject_same_predicate_is_not_a_contradiction(
    scenario: tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]],
) -> None:
    """The exact scenario from F-013: same predicate, different subject,
    different value — must NOT be flagged."""
    from raasoa.models.claim import Claim
    from raasoa.quality.claim_conflicts import detect_claim_conflicts

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        new_claim = Claim(
            tenant_id=ids["tenant_id"], document_id=ids["doc_new"],
            subject="IT dept", predicate="response time", object_value="4h",
            confidence=0.9, evidence_span="evidence", status="active",
        )
        session.add(new_claim)
        await session.commit()

        conflicts = await detect_claim_conflicts(
            session, ids["doc_new"], ids["tenant_id"],
            [new_claim], _SamePredicateEmbeddingProvider(),
        )

    assert conflicts == []


async def test_same_subject_same_predicate_different_value_is_flagged(
    scenario: tuple[dict[str, uuid.UUID], async_sessionmaker[AsyncSession]],
) -> None:
    """Sanity check: the fix doesn't over-correct — a genuine
    contradiction (same subject, same predicate, different value) must
    still be detected."""
    from raasoa.models.claim import Claim
    from raasoa.quality.claim_conflicts import detect_claim_conflicts

    ids, sessionmaker = scenario
    async with sessionmaker() as session:
        new_claim = Claim(
            tenant_id=ids["tenant_id"], document_id=ids["doc_new"],
            subject="HR dept", predicate="response time", object_value="4h",
            confidence=0.9, evidence_span="evidence", status="active",
        )
        session.add(new_claim)
        await session.commit()

        conflicts = await detect_claim_conflicts(
            session, ids["doc_new"], ids["tenant_id"],
            [new_claim], _SamePredicateEmbeddingProvider(),
        )

    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == "claim_contradiction"
    # F-013's second fix: the claim id is now stored for claim-level
    # (not whole-document) auto-resolution.
    assert conflicts[0].details["new_claim"]["id"] == str(new_claim.id)
