"""E2E ACL enforcement tests for the 4 MCP-backing document endpoints.

GET /v1/documents, GET /v1/documents/{id}, POST /v1/find_by_metadata, and
GET /v1/documents/{id}/dependencies (+ /v1/dependencies/graph) previously
applied zero ACL/principal filtering — any caller could enumerate every
document via these paths regardless of a source's restricted visibility.

Requires a live Postgres. Skips gracefully when unreachable.

Uses the app's own global raasoa.db engine throughout (no private
per-test engine, no dependency_overrides): resolve_principal_async()'s
API-key lookup (_resolve_key_row_from_db) always opens its own session
from that global engine internally, regardless of any get_session
override — mixing a private test engine with that unavoidable internal
usage is what causes cross-loop asyncpg errors. Using only the global
engine, consistent with how the app actually runs, avoids the conflict.
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


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    """Dispose raasoa.db.engine's pool before AND after each test.

    raasoa.db.engine is a module-level singleton whose asyncpg
    connections are loop-bound, but pytest-asyncio's default event loop
    is function-scoped (a fresh loop per test). Without disposing, a
    pooled connection opened on one test's loop can be checked out again
    during a later test (now running on a different, or already-closed,
    loop), raising "attached to a different loop" / "Event loop is
    closed" from connection cleanup code unrelated to the test's own
    logic. Disposing before AND after forces fresh, loop-correct
    connections every time and leaves a clean pool for whatever test
    file runs next.
    """
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def personal_key_scenario() -> AsyncGenerator[dict[str, object], None]:
    """A tenant with AUTH_ENABLED, one personal API key for "Jane"
    (principal_id=user:jane), an open source, a restricted source with a
    document Jane has an explicit grant on, and a second restricted
    document she has NO grant on."""
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
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'A5PersonalTest')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO api_keys "
                "(id, tenant_id, key_hash, key_prefix, name, principal_id, "
                " clearance, is_admin, is_active) "
                "VALUES (:id, :tid, :hash, :prefix, 'Jane Personal Key', "
                " 'user:jane', 'public', false, true)"
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
            (doc_open, src_open, "Personal Open Doc"),
            (doc_granted, src_restricted, "Personal Restricted Granted"),
            (doc_ungranted, src_restricted, "Personal Restricted Ungranted"),
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
                    "soid": f"pa5-{doc_id.hex[:6]}", "title": title,
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
        "tenant_id": tenant_id, "doc_open": doc_open,
        "doc_granted": doc_granted, "doc_ungranted": doc_ungranted,
        "headers": {"Authorization": f"Bearer {raw_key}"},
    }

    config_module.settings.auth_enabled = original_auth_enabled
    async with async_session() as session:
        doc_ids = [doc_open, doc_granted, doc_ungranted]
        await session.execute(
            sql_text("DELETE FROM acl_entries WHERE document_id = ANY(:ids)"), {"ids": doc_ids},
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


async def _client() -> AsyncClient:
    from raasoa.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_documents_excludes_ungranted_restricted(
    personal_key_scenario: dict[str, object],
) -> None:
    async with await _client() as client:
        resp = await client.get(
            "/v1/documents", params={"limit": 200}, headers=personal_key_scenario["headers"],
        )
    assert resp.status_code == 200
    titles = {d["title"] for d in resp.json()["items"] if "Personal" in d["title"]}
    assert titles == {"Personal Open Doc", "Personal Restricted Granted"}


async def test_get_document_granted_is_200_ungranted_is_404(
    personal_key_scenario: dict[str, object],
) -> None:
    """A 404, not 403, for the ungranted document — its existence isn't
    leaked to a principal with no grant."""
    headers = personal_key_scenario["headers"]
    async with await _client() as client:
        resp = await client.get(
            f"/v1/documents/{personal_key_scenario['doc_granted']}", headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get(
            f"/v1/documents/{personal_key_scenario['doc_ungranted']}", headers=headers,
        )
        assert resp.status_code == 404


async def test_find_by_metadata_excludes_ungranted_restricted(
    personal_key_scenario: dict[str, object],
) -> None:
    async with await _client() as client:
        resp = await client.post(
            "/v1/find_by_metadata", json={"metadata": {}, "limit": 200},
            headers=personal_key_scenario["headers"],
        )
    assert resp.status_code == 200
    titles = {d["title"] for d in resp.json()["documents"] if "Personal" in d["title"]}
    assert titles == {"Personal Open Doc", "Personal Restricted Granted"}


async def test_dependency_graph_excludes_ungranted_restricted_node(
    personal_key_scenario: dict[str, object],
) -> None:
    async with await _client() as client:
        resp = await client.get(
            "/v1/dependencies/graph", params={"limit_nodes": 500},
            headers=personal_key_scenario["headers"],
        )
    assert resp.status_code == 200
    titles = {n["title"] for n in resp.json()["nodes"] if "Personal" in n["title"]}
    assert titles == {"Personal Open Doc", "Personal Restricted Granted"}


async def test_get_dependencies_hides_ungranted_sibling(
    personal_key_scenario: dict[str, object],
) -> None:
    """A caller with access to the granted restricted document must not
    see the ungranted sibling from the same source in its dependency
    list."""
    headers = personal_key_scenario["headers"]
    async with await _client() as client:
        resp = await client.get(
            f"/v1/documents/{personal_key_scenario['doc_granted']}/dependencies", headers=headers,
        )
    assert resp.status_code == 200
    sibling_titles = {s["title"] for s in resp.json()["dependencies"]["same_source"]}
    assert "Personal Restricted Ungranted" not in sibling_titles
