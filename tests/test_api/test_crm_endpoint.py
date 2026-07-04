"""E2E test: POST /v1/crm/query wires resolve_principal_async + the CRM
DSL together correctly through the actual HTTP layer (the pure-function
DSL/ACL semantics are covered exhaustively in
tests/test_retrieval/test_crm_query.py — this only proves the endpoint
wiring itself: auth, validation-error surfacing, response shape).

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import hashlib
import json
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
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def crm_scenario() -> AsyncGenerator[dict[str, object], None]:
    import raasoa.config as config_module
    from raasoa.db import async_session

    original_auth_enabled = config_module.settings.auth_enabled
    config_module.settings.auth_enabled = True

    tenant_id = uuid.uuid4()
    src_open = uuid.uuid4()
    src_restricted = uuid.uuid4()
    raw_key = "sk-test-" + secrets.token_hex(16)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'CrmEndpointTest')"),
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
                "VALUES (:id, :tid, 'hubspot', 'Open', '{}'::jsonb, 'inherit')"
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
        await session.execute(
            sql_text(
                "INSERT INTO crm_objects "
                "(id, tenant_id, source_id, object_type, external_id, "
                " owner_principal_id, properties) "
                "VALUES (:id, :tid, :sid, 'deals', 'd1', NULL, CAST(:props AS jsonb))"
            ),
            {
                "id": uuid.uuid4(), "tid": tenant_id, "sid": src_open,
                "props": json.dumps({"dealname": "Open Deal", "amount": "1000"}),
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO crm_objects "
                "(id, tenant_id, source_id, object_type, external_id, "
                " owner_principal_id, properties) "
                "VALUES (:id, :tid, :sid, 'deals', 'd2', 'user:someone-else', "
                " CAST(:props AS jsonb))"
            ),
            {
                "id": uuid.uuid4(), "tid": tenant_id, "sid": src_restricted,
                "props": json.dumps({"dealname": "Hidden Deal", "amount": "9999"}),
            },
        )
        await session.commit()

    yield {"tenant_id": tenant_id, "headers": {"Authorization": f"Bearer {raw_key}"}}

    config_module.settings.auth_enabled = original_auth_enabled
    async with async_session() as session:
        await session.execute(
            sql_text("DELETE FROM crm_objects WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM api_keys WHERE tenant_id = :tid"), {"tid": tenant_id},
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


async def test_crm_query_auto_scopes_to_caller(crm_scenario: dict[str, object]) -> None:
    async with await _client() as client:
        resp = await client.post(
            "/v1/crm/query", json={"object_type": "deals"}, headers=crm_scenario["headers"],
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["properties"]["dealname"] == "Open Deal"


async def test_crm_query_rejects_invalid_operator(crm_scenario: dict[str, object]) -> None:
    async with await _client() as client:
        resp = await client.post(
            "/v1/crm/query",
            json={"object_type": "deals", "filters": [{"field": "amount", "op": "bogus"}]},
            headers=crm_scenario["headers"],
        )
    assert resp.status_code == 422


async def test_crm_query_without_credentials_is_401(crm_scenario: dict[str, object]) -> None:
    async with await _client() as client:
        resp = await client.post("/v1/crm/query", json={"object_type": "deals"})
    assert resp.status_code == 401
