"""Regression test: enabling llm_judge_enabled must not break ingestion.

Found while flipping llm_judge_enabled's default to True (2026-07-06, see
AUDIT_AND_FIX_PLAN.md §5 question 2): step 14 of ingest_file checked
``doc.conflict_status == "conflicts_detected"`` directly on the ORM
object. detect_claim_conflicts sets conflict_status via a raw SQL UPDATE
(bypassing the ORM identity map), and the several session.commit() calls
earlier in ingest_file expire doc's attributes by default — accessing an
expired attribute triggers SQLAlchemy's implicit lazy-reload, which is
not supported under AsyncSession and raises
``sqlalchemy.exc.MissingGreenlet``, 500-ing every single ingest that
reaches step 14 with llm_judge_enabled on. This was completely dormant
while the setting defaulted to False, since Python's ``and`` short-circuit
meant ``doc.conflict_status`` was never evaluated at all.

The fix: ingest_file now does an explicit ``await session.refresh(doc)``
before reading conflict_status. This test proves it by mocking claim
extraction/conflict-detection/judge to their minimal side effects (avoids
needing a real LLM call for any of the three) and asserting ingestion
completes successfully with llm_judge_enabled=True, instead of 500ing.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

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
    model_id = "test-stub"
    dimensions = 768

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        return [[0.0] * 768 for _ in texts]


@pytest.fixture
async def tenant_id() -> AsyncGenerator[uuid.UUID, None]:
    from raasoa.db import async_session
    from raasoa.middleware.auth import DEFAULT_TENANT

    tid = DEFAULT_TENANT
    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM tenants WHERE id = :tid"), {"tid": tid},
        )
        if not result.first():
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Default Tenant')"),
                {"id": tid},
            )
            await session.commit()

    before = set()
    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM documents WHERE tenant_id = :tid"), {"tid": tid},
        )
        before = {row.id for row in result.fetchall()}

    yield tid

    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM documents WHERE tenant_id = :tid"), {"tid": tid},
        )
        new_doc_ids = list({row.id for row in result.fetchall()} - before)
        if new_doc_ids:
            await session.execute(
                sql_text("DELETE FROM chunks WHERE document_id = ANY(:ids)"),
                {"ids": new_doc_ids},
            )
            await session.execute(
                sql_text("DELETE FROM documents WHERE id = ANY(:ids)"),
                {"ids": new_doc_ids},
            )
            await session.commit()


async def test_ingest_with_llm_judge_enabled_and_conflict_detected_does_not_500(
    tenant_id: uuid.UUID,
) -> None:
    from raasoa.ingestion.pipeline import ingest_file

    async def _fake_extract_and_store_claims(
        *, session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, chunks: list,
    ) -> list[dict]:
        del session, tenant_id, chunks
        return [{"id": str(uuid.uuid4()), "predicate": "fake"}]

    async def _fake_detect_claim_conflicts(
        *, session: AsyncSession, document_id: uuid.UUID, tenant_id: uuid.UUID,
        new_claims: list, embedding_provider,
    ) -> None:
        del tenant_id, new_claims, embedding_provider
        # Mirrors the real function's actual side effect: a raw SQL
        # UPDATE that bypasses the ORM identity map entirely.
        await session.execute(
            sql_text(
                "UPDATE documents SET conflict_status = 'conflicts_detected' "
                "WHERE id = :did"
            ),
            {"did": document_id},
        )
        await session.commit()

    async def _fake_auto_resolve_conflicts(
        session: AsyncSession, tenant_id: uuid.UUID, threshold: float | None = None,
    ) -> dict:
        del session, tenant_id, threshold
        return {"total_open": 0, "judged": 0, "auto_resolved": 0, "kept_for_human": 0}

    with (
        patch.object(settings, "llm_judge_enabled", True),
        patch.object(settings, "conflict_detection_enabled", True),
        patch.object(settings, "claim_extraction_enabled", True),
        patch(
            "raasoa.quality.claims.extract_and_store_claims",
            new=_fake_extract_and_store_claims,
        ),
        patch(
            "raasoa.quality.claim_conflicts.detect_claim_conflicts",
            new=_fake_detect_claim_conflicts,
        ),
        patch(
            "raasoa.quality.judge.auto_resolve_conflicts",
            new=_fake_auto_resolve_conflicts,
        ),
    ):
        from raasoa.db import async_session

        async with async_session() as session:
            doc, _assessment = await ingest_file(
                session=session,
                tenant_id=tenant_id,
                source_id=await _get_or_create_source(session, tenant_id),
                file_data=b"Content for the llm-judge pipeline regression test, over 50 chars.",
                filename=f"llm-judge-test-{uuid.uuid4().hex[:8]}.txt",
                embedding_provider=_ZeroVectorProvider(),
            )

        assert doc is not None
        assert doc.id is not None


async def _get_or_create_source(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    result = await session.execute(
        sql_text(
            "SELECT id FROM sources WHERE tenant_id = :tid AND source_type = 'upload'"
        ),
        {"tid": tenant_id},
    )
    row = result.first()
    if row:
        return row.id
    source_id = uuid.uuid4()
    await session.execute(
        sql_text(
            "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
            "VALUES (:id, :tid, 'upload', 'Uploads', '{}'::jsonb)"
        ),
        {"id": source_id, "tid": tenant_id},
    )
    await session.commit()
    return source_id
