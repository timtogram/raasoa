"""E2E test: POST /v1/retrieve auto-scopes results to the caller's
resolved principal — the final piece of the ACL/RBAC wiring (A6).

A personal API key's search results are automatically restricted without
the caller needing to pass anything; a legacy/tenant-wide key keeps
today's unfiltered behavior. POST /v1/answer shares the exact same
search()-call-and-principal-resolution code path (not tested separately
here to avoid a slow/Ollama-dependent test in the regular suite — see
INTEGRATIONS.md or the manual verification in this feature's commit
message for an end-to-end proof including real answer synthesis).

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sql_text

from raasoa.config import settings

DATABASE_URL = settings.database_url


def _db_reachable() -> bool:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

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


class _DistinctVectorProvider:
    model_id = "test-stub"
    dimensions = 768

    def __init__(self) -> None:
        self._counter = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for _ in texts:
            self._counter += 1
            vec = [0.0] * 768
            vec[self._counter % 768] = 1.0
            vectors.append(vec)
        return vectors


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    """See tests/test_api/test_documents_acl.py for why this is needed:
    raasoa.db.engine is a loop-bound singleton, pytest-asyncio's default
    loop is function-scoped, and disposing before/after each test avoids
    "attached to a different loop" errors from stale pooled connections.
    """
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def retrieve_scenario() -> AsyncGenerator[dict[str, object], None]:
    import raasoa.config as config_module
    from raasoa.db import async_session

    original_auth_enabled = config_module.settings.auth_enabled
    config_module.settings.auth_enabled = True

    tenant_id = uuid.uuid4()
    src_open = uuid.uuid4()
    src_restricted = uuid.uuid4()
    doc_open = uuid.uuid4()
    doc_granted = uuid.uuid4()
    doc_ungranted = uuid.uuid4()
    raw_key = "sk-test-" + secrets.token_hex(16)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'A6RetrieveTest')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO api_keys "
                "(id, tenant_id, key_hash, key_prefix, name, principal_id, "
                " clearance, is_admin, is_active) "
                "VALUES (:id, :tid, :hash, :prefix, 'Jane', 'user:jane', "
                " 'public', false, true)"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "hash": key_hash, "prefix": raw_key[:10]},
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
            (doc_open, src_open, "A6 Open Doc"),
            (doc_granted, src_restricted, "A6 Restricted Granted"),
            (doc_ungranted, src_restricted, "A6 Restricted Ungranted"),
        ]:
            txt = "widget policy allowance content"
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
                    "soid": f"a6-{doc_id.hex[:6]}", "title": title,
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
                    "id": uuid.uuid4(), "did": doc_id,
                    "hash": hashlib.sha256(txt.encode()).digest(),
                    "text": txt, "emb": str([0.0] * 768),
                },
            )
        await session.execute(
            sql_text(
                "INSERT INTO acl_entries "
                "(id, document_id, principal_type, principal_id, permission) "
                "VALUES (:id, :did, 'user', 'user:jane', 'read')"
            ),
            {"id": uuid.uuid4(), "did": doc_granted},
        )
        await session.commit()

    yield {
        "tenant_id": tenant_id,
        "headers": {"Authorization": f"Bearer {raw_key}"},
    }

    config_module.settings.auth_enabled = original_auth_enabled
    async with async_session() as session:
        doc_ids = [doc_open, doc_granted, doc_ungranted]
        await session.execute(
            sql_text("DELETE FROM acl_entries WHERE document_id = ANY(:ids)"), {"ids": doc_ids},
        )
        await session.execute(
            sql_text("DELETE FROM chunks WHERE document_id = ANY(:ids)"), {"ids": doc_ids},
        )
        await session.execute(
            sql_text("DELETE FROM api_keys WHERE tenant_id = :tid"), {"tid": tenant_id},
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


async def test_retrieve_auto_scopes_to_personal_principal(
    retrieve_scenario: dict[str, object],
) -> None:
    from unittest.mock import patch

    from raasoa.main import app

    with patch(
        "raasoa.providers.factory.get_embedding_provider",
        return_value=_DistinctVectorProvider(),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/retrieve",
                json={"query": "widget policy allowance", "top_k": 10},
                headers=retrieve_scenario["headers"],
            )
    assert resp.status_code == 200
    titles = {
        h["document_title"] for h in resp.json()["results"]
        if h.get("document_title", "").startswith("A6")
    }
    assert titles == {"A6 Open Doc", "A6 Restricted Granted"}


async def test_retrieve_without_credentials_is_401_when_auth_enabled(
    retrieve_scenario: dict[str, object],
) -> None:
    from raasoa.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/retrieve", json={"query": "widget policy allowance", "top_k": 10},
        )
    assert resp.status_code == 401
