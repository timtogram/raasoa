"""E2E tests for LLM-judge auto-resolve claim-level scoping (F-013).

Before this fix, auto_resolve_conflicts superseded the ENTIRE losing
document — every claim it carried, not just the one that actually
conflicted — and flipped documents.review_status to 'superseded',
removing the whole document from hybrid search. A document with 99
valid claims and one disputed predicate would have all 100 wiped from
search over a single high-confidence judge verdict.

Requires a live Postgres. Skips gracefully when unreachable. Ollama is
mocked via httpx.MockTransport — no real LLM call.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from raasoa.config import settings

DATABASE_URL = settings.database_url
_REAL_ASYNC_CLIENT = httpx.AsyncClient


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


def _mock_ollama_judge(recommendation: str, confidence: float) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps({
            "recommendation": recommendation,
            "confidence": confidence,
            "reasoning": "test verdict",
        })
        return httpx.Response(200, json={"response": body})

    return httpx.MockTransport(handler)


@pytest.fixture
async def scenario() -> AsyncGenerator[
    tuple[dict[str, object], async_sessionmaker[AsyncSession]], None,
]:
    """Two documents, each carrying TWO claims. Only one claim per
    document is part of the conflict — the other must survive
    auto-resolution untouched if the fix is scoped correctly."""
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ids: dict[str, object] = {
        "tenant_id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "doc_a": uuid.uuid4(),
        "doc_b": uuid.uuid4(),
        "claim_a_disputed": uuid.uuid4(),
        "claim_a_safe": uuid.uuid4(),
        "claim_b_disputed": uuid.uuid4(),
        "claim_b_safe": uuid.uuid4(),
        "conflict_id": uuid.uuid4(),
    }

    async with sessionmaker() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'JudgeScopingTest')"),
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
        for doc_id, title in [(ids["doc_a"], "Doc A (2026)"), (ids["doc_b"], "Doc B (2024)")]:
            await session.execute(
                sql_text(
                    "INSERT INTO documents "
                    "(id, tenant_id, source_id, source_object_id, title, status, "
                    " review_status, version, chunk_count, access_count) "
                    "VALUES (:id, :tid, :sid, :soid, :title, 'indexed', "
                    " 'published', 1, 1, 0)"
                ),
                {
                    "id": doc_id, "tid": ids["tenant_id"], "sid": ids["source_id"],
                    "soid": f"js-{doc_id.hex[:6]}", "title": title,
                },
            )
        for claim_id, doc_id, predicate, value in [
            (ids["claim_a_disputed"], ids["doc_a"], "meal allowance", "32 EUR"),
            (ids["claim_a_safe"], ids["doc_a"], "vacation days", "30"),
            (ids["claim_b_disputed"], ids["doc_b"], "meal allowance", "25 EUR"),
            (ids["claim_b_safe"], ids["doc_b"], "vacation days", "28"),
        ]:
            await session.execute(
                sql_text(
                    "INSERT INTO claims "
                    "(id, tenant_id, document_id, subject, predicate, object_value, "
                    " confidence, evidence_span, status) "
                    "VALUES (:id, :tid, :did, 'Acme', :pred, :val, "
                    " 0.9, 'evidence', 'active')"
                ),
                {
                    "id": claim_id, "tid": ids["tenant_id"], "did": doc_id,
                    "pred": predicate, "val": value,
                },
            )
        # The conflict references only the two DISPUTED claims.
        details = {
            "new_claim": {
                "id": str(ids["claim_a_disputed"]), "subject": "Acme",
                "predicate": "meal allowance", "value": "32 EUR", "evidence": "ev",
            },
            "existing_claim": {
                "id": str(ids["claim_b_disputed"]), "subject": "Acme",
                "predicate": "meal allowance", "value": "25 EUR", "evidence": "ev",
            },
            "predicate_similarity": 0.95,
            "new_doc_id": str(ids["doc_a"]),
            "existing_doc_title": "Doc B (2024)",
        }
        await session.execute(
            sql_text(
                "INSERT INTO conflict_candidates "
                "(id, tenant_id, document_a_id, document_b_id, conflict_type, "
                " confidence, details, status) "
                "VALUES (:id, :tid, :a, :b, 'claim_contradiction', 0.9, "
                " CAST(:details AS jsonb), 'new')"
            ),
            {
                "id": ids["conflict_id"], "tid": ids["tenant_id"],
                "a": ids["doc_a"], "b": ids["doc_b"], "details": json.dumps(details),
            },
        )
        await session.commit()

    yield ids, sessionmaker

    async with sessionmaker() as session:
        await session.execute(
            sql_text("DELETE FROM review_tasks WHERE tenant_id = :tid"), {"tid": ids["tenant_id"]},
        )
        await session.execute(
            sql_text("DELETE FROM conflict_candidates WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
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


async def test_auto_resolve_supersedes_only_the_disputed_claim(
    scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact regression from F-013: 'keep_a' (doc A's claim wins)
    must supersede ONLY claim_b_disputed — not doc B's other claim, and
    not doc B's review_status."""
    from raasoa.quality.judge import auto_resolve_conflicts

    ids, sessionmaker = scenario
    transport = _mock_ollama_judge("keep_a", 0.95)

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    async with sessionmaker() as session:
        stats = await auto_resolve_conflicts(session, ids["tenant_id"])  # type: ignore[arg-type]

    assert stats["auto_resolved"] == 1

    async with sessionmaker() as session:
        claims_result = await session.execute(
            sql_text("SELECT id, status FROM claims WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
        )
        claim_status = {r.id: r.status for r in claims_result.fetchall()}

        docs_result = await session.execute(
            sql_text("SELECT id, review_status FROM documents WHERE tenant_id = :tid"),
            {"tid": ids["tenant_id"]},
        )
        doc_status = {r.id: r.review_status for r in docs_result.fetchall()}

    # Only the disputed losing claim is superseded.
    assert claim_status[ids["claim_b_disputed"]] == "superseded"
    # The winning claim and BOTH documents' other claims survive.
    assert claim_status[ids["claim_a_disputed"]] == "active"
    assert claim_status[ids["claim_a_safe"]] == "active"
    assert claim_status[ids["claim_b_safe"]] == "active"
    # Neither document is superseded at the document level.
    assert doc_status[ids["doc_a"]] == "published"
    assert doc_status[ids["doc_b"]] == "published"


async def test_conflict_without_claim_ids_is_kept_for_human(
    scenario: tuple[dict[str, object], async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older-format conflict (no claim ids in `details`) must never
    fall back to the unsafe whole-document behavior — it's left for a
    human instead of guessed at."""
    from raasoa.quality.judge import auto_resolve_conflicts

    ids, sessionmaker = scenario
    transport = _mock_ollama_judge("keep_a", 0.95)

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    async with sessionmaker() as session:
        await session.execute(
            sql_text(
                "UPDATE conflict_candidates SET details = CAST(:details AS jsonb) "
                "WHERE id = :cid"
            ),
            {
                "cid": ids["conflict_id"],
                "details": json.dumps({
                    "new_claim": {"subject": "Acme", "predicate": "meal allowance"},
                    "existing_claim": {"subject": "Acme", "predicate": "meal allowance"},
                }),
            },
        )
        await session.commit()

        stats = await auto_resolve_conflicts(session, ids["tenant_id"])  # type: ignore[arg-type]

    assert stats["auto_resolved"] == 0
    assert stats["kept_for_human"] == 1

    async with sessionmaker() as session:
        claims_result = await session.execute(
            sql_text(
                "SELECT status FROM claims WHERE id = ANY(:ids)"
            ),
            {"ids": [ids["claim_a_disputed"], ids["claim_b_disputed"]]},
        )
        statuses = {r.status for r in claims_result.fetchall()}

    assert statuses == {"active"}
