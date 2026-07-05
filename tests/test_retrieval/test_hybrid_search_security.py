"""Regression test for a SQL-injection vector in metadata_filter handling.

``hybrid_search()`` and ``find_by_metadata`` previously built the JSONB key
side of ``doc_metadata->>'{key}'`` via an f-string. A crafted key could break
out of the string literal and neutralize subsequent AND-ed clauses (e.g. an
ACL check appended after the metadata filter) via a trailing SQL comment.
Both call sites now bind the key as a parameter — this test proves that
empirically against a real Postgres, not just by reading the source.

Requires a live Postgres (the same one CI runs migrations against). Skips
gracefully when unreachable, matching tests/test_mcp/test_mcp_stdio.py.

Each test uses its OWN private AsyncEngine rather than the app's global
``raasoa.db.engine`` singleton. asyncpg connections are loop-bound, and
pytest-asyncio gives each test function a fresh event loop by default;
reusing one process-wide pooled engine across tests reliably raises
"attached to a different loop" / "Event loop is closed" once more than one
test in the suite touches it. A private per-test engine sidesteps this
entirely. For the two HTTP-level tests, the private engine's session is
wired in via FastAPI's ``dependency_overrides`` — the standard pattern for
testing an app that normally depends on a global DB session.
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

# A malicious metadata_filter KEY. If it were ever spliced into the SQL text
# again, this breaks out of the `->>'...'` literal and comments out anything
# that follows in the same WHERE clause.
INJECTION_KEY = "x' = 'y' OR '1'='1' -- "


class _ZeroVectorProvider:
    """Minimal EmbeddingProvider stub — no network, no real embeddings."""

    model_id = "test-stub"
    dimensions = 768

    async def embed(
        self, texts: list[str], *, input_type: str = "search_document"
    ) -> list[list[float]]:
        del input_type
        return [[0.0] * 768 for _ in texts]


async def _seed_fixture(
    session: AsyncSession, tenant_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a tenant (if absent) + source + document + chunk. Returns
    (source_id, document_id)."""
    source_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    chunk_text_val = "The meal allowance is 32 EUR per travel day."

    existing = await session.execute(
        sql_text("SELECT 1 FROM tenants WHERE id = :id"), {"id": tenant_id},
    )
    if not existing.first():
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": "SecTest Tenant"},
        )
    await session.execute(
        sql_text(
            "INSERT INTO sources (id, tenant_id, source_type, name, connection_config) "
            "VALUES (:id, :tid, 'file_upload', 'Upload', '{}'::jsonb)"
        ),
        {"id": source_id, "tid": tenant_id},
    )
    await session.execute(
        sql_text(
            "INSERT INTO documents "
            "(id, tenant_id, source_id, source_object_id, title, status, "
            " version, chunk_count, access_count, doc_metadata) "
            "VALUES (:id, :tid, :sid, :soid, 'Travel Policy', 'indexed', "
            " 1, 1, 0, :meta)"
        ),
        {
            "id": doc_id, "tid": tenant_id, "sid": source_id,
            "soid": f"sectest-{doc_id.hex[:8]}",
            "meta": '{"ampel": "gruen"}',
        },
    )
    await session.execute(
        sql_text(
            "INSERT INTO chunks "
            "(id, document_id, chunk_index, content_hash, chunk_text, "
            " token_count, embedding, tsv) "
            "VALUES (:id, :did, 0, :hash, :text, 8, :emb, "
            " to_tsvector('simple', :text))"
        ),
        {
            "id": uuid.uuid4(), "did": doc_id,
            "hash": hashlib.sha256(chunk_text_val.encode()).digest(),
            "text": chunk_text_val,
            "emb": str([0.0] * 768),
        },
    )
    await session.commit()
    return source_id, doc_id


async def _cleanup_fixture(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    doc_id: uuid.UUID,
    *,
    drop_tenant: bool,
) -> None:
    await session.execute(sql_text("DELETE FROM chunks WHERE document_id = :did"), {"did": doc_id})
    await session.execute(sql_text("DELETE FROM documents WHERE id = :did"), {"did": doc_id})
    await session.execute(sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id})
    if drop_tenant:
        await session.execute(sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    await session.commit()


@pytest.fixture
async def private_engine() -> AsyncGenerator[
    tuple[create_async_engine, async_sessionmaker[AsyncSession]], None,
]:
    """A private engine, scoped to exactly one test's event loop."""
    engine = create_async_engine(DATABASE_URL)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield engine, sessionmaker
    await engine.dispose()


async def test_metadata_filter_injection_key_matches_nothing(
    private_engine: tuple[create_async_engine, async_sessionmaker[AsyncSession]],
) -> None:
    """A crafted metadata_filter key must be treated as a literal, inert
    string — never restructure the query or match unrelated rows."""
    from raasoa.retrieval.hybrid_search import search

    _engine, sessionmaker = private_engine
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        source_id, doc_id = await _seed_fixture(session, tenant_id)
        try:
            results = await search(
                session=session,
                query="meal allowance",
                tenant_id=tenant_id,
                embedding_provider=_ZeroVectorProvider(),
                top_k=5,
                metadata_filter={INJECTION_KEY: "gruen"},
            )
            assert results == []
        finally:
            await _cleanup_fixture(session, tenant_id, source_id, doc_id, drop_tenant=True)


async def test_metadata_filter_normal_key_still_matches(
    private_engine: tuple[create_async_engine, async_sessionmaker[AsyncSession]],
) -> None:
    """Positive control: the parameterized fix must not silently break
    legitimate filtering (a naive 'always return nothing' bug would also
    pass the injection test above without this)."""
    from raasoa.retrieval.hybrid_search import search

    _engine, sessionmaker = private_engine
    tenant_id = uuid.uuid4()

    async with sessionmaker() as session:
        source_id, doc_id = await _seed_fixture(session, tenant_id)
        try:
            results = await search(
                session=session,
                query="meal allowance",
                tenant_id=tenant_id,
                embedding_provider=_ZeroVectorProvider(),
                top_k=5,
                metadata_filter={"ampel": "gruen"},
            )
            assert any(r.document_id == doc_id for r in results)
        finally:
            await _cleanup_fixture(session, tenant_id, source_id, doc_id, drop_tenant=True)


async def test_find_by_metadata_injection_key_matches_nothing(
    private_engine: tuple[create_async_engine, async_sessionmaker[AsyncSession]],
) -> None:
    """Same regression, for the find_by_metadata endpoint's own (separate)
    f-string-turned-parameterized JSONB key handling.

    Uses AUTH_ENABLED=false's DEFAULT_TENANT (resolve_tenant_async always
    resolves to it in that mode — there's no per-request tenant override in
    this codebase) and wires the private per-test engine into the app via
    dependency_overrides, so this HTTP-level test never touches the app's
    global (loop-affine, cross-test-shared) DB engine.
    """
    from httpx import ASGITransport, AsyncClient

    from raasoa.db import get_session
    from raasoa.main import app
    from raasoa.middleware.auth import DEFAULT_TENANT

    _engine, sessionmaker = private_engine

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    try:
        async with sessionmaker() as session:
            source_id, doc_id = await _seed_fixture(session, DEFAULT_TENANT)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/find_by_metadata",
                    json={"metadata": {INJECTION_KEY: "gruen"}, "limit": 20},
                )
            assert resp.status_code == 200
            assert resp.json()["documents"] == []
        finally:
            async with sessionmaker() as session:
                await _cleanup_fixture(
                    session, DEFAULT_TENANT, source_id, doc_id, drop_tenant=False,
                )
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_find_by_metadata_normal_key_still_matches(
    private_engine: tuple[create_async_engine, async_sessionmaker[AsyncSession]],
) -> None:
    from httpx import ASGITransport, AsyncClient

    from raasoa.db import get_session
    from raasoa.main import app
    from raasoa.middleware.auth import DEFAULT_TENANT

    _engine, sessionmaker = private_engine

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    try:
        async with sessionmaker() as session:
            source_id, doc_id = await _seed_fixture(session, DEFAULT_TENANT)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/find_by_metadata",
                    json={"metadata": {"ampel": "gruen"}, "limit": 20},
                )
            assert resp.status_code == 200
            docs = resp.json()["documents"]
            assert any(d["id"] == str(doc_id) for d in docs)
        finally:
            async with sessionmaker() as session:
                await _cleanup_fixture(
                    session, DEFAULT_TENANT, source_id, doc_id, drop_tenant=False,
                )
    finally:
        app.dependency_overrides.pop(get_session, None)
