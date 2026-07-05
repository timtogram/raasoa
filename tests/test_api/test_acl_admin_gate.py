"""Regression tests for the /v1/acl privilege-escalation fix.

Before this fix, create_acl_entry/list_acl_entries/delete_acl_entry only
resolved the caller's tenant — any valid tenant key (including a
non-admin personal key) could grant itself `admin` on any document in
the tenant. These endpoints must be gated the same way as
update_source_visibility: require_admin (admin-capable caller AND
tenant opted into the admin API).

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


@pytest.fixture(autouse=True)
async def _reset_engine_pool_per_test() -> AsyncGenerator[None, None]:
    from raasoa.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def _client() -> AsyncClient:
    from raasoa.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def acl_scenario() -> AsyncGenerator[dict[str, object], None]:
    """A fresh tenant with AUTH_ENABLED, one legacy master key, one
    non-admin personal key, admin_api_enabled=false, and one document."""
    import raasoa.config as config_module
    from raasoa.db import async_session

    original_auth_enabled = config_module.settings.auth_enabled
    config_module.settings.auth_enabled = True

    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    raw_master_key = "sk-master-" + secrets.token_hex(16)
    master_key_hash = hashlib.sha256(raw_master_key.encode()).hexdigest()
    raw_personal_key = "sk-personal-" + secrets.token_hex(16)
    personal_key_hash = hashlib.sha256(raw_personal_key.encode()).hexdigest()

    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'AclGateTest')"),
            {"id": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO api_keys "
                "(id, tenant_id, key_hash, key_prefix, name, is_active) "
                "VALUES (:id, :tid, :hash, :prefix, 'Master', true)"
            ),
            {
                "id": uuid.uuid4(), "tid": tenant_id,
                "hash": master_key_hash, "prefix": raw_master_key[:10],
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO api_keys "
                "(id, tenant_id, key_hash, key_prefix, name, principal_id, is_active) "
                "VALUES (:id, :tid, :hash, :prefix, 'Bob', 'user:bob', true)"
            ),
            {
                "id": uuid.uuid4(), "tid": tenant_id,
                "hash": personal_key_hash, "prefix": raw_personal_key[:10],
            },
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'file_upload', 'Restricted Source', '{}'::jsonb)"
            ),
            {"id": source_id, "tid": tenant_id},
        )
        await session.execute(
            sql_text(
                "INSERT INTO documents "
                "(id, tenant_id, source_id, source_object_id, title, status, "
                " review_status, version, chunk_count, access_count) "
                "VALUES (:id, :tid, :sid, :soid, 'Secret Doc', 'indexed', "
                " 'published', 1, 1, 0)"
            ),
            {
                "id": document_id, "tid": tenant_id, "sid": source_id,
                "soid": str(document_id),
            },
        )
        await session.commit()

    yield {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "master_headers": {"Authorization": f"Bearer {raw_master_key}"},
        "personal_headers": {"Authorization": f"Bearer {raw_personal_key}"},
    }

    config_module.settings.auth_enabled = original_auth_enabled
    async with async_session() as session:
        await session.execute(
            sql_text("DELETE FROM acl_entries WHERE document_id = :did"),
            {"did": document_id},
        )
        await session.execute(
            sql_text("DELETE FROM documents WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM sources WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM api_keys WHERE tenant_id = :tid"), {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id},
        )
        await session.commit()


async def test_personal_key_cannot_self_grant_acl(
    acl_scenario: dict[str, object],
) -> None:
    """The exact escalation from F-002: a non-admin personal key must not
    be able to grant itself access via POST /v1/acl."""
    async with await _client() as client:
        resp = await client.post(
            "/v1/acl",
            json={
                "document_id": str(acl_scenario["document_id"]),
                "principal_type": "user",
                "principal_id": "user:bob",
                "permission": "admin",
            },
            headers=acl_scenario["personal_headers"],
        )
    assert resp.status_code == 403


async def test_master_key_blocked_until_admin_api_enabled(
    acl_scenario: dict[str, object],
) -> None:
    """Even the tenant's own master key can't manage ACLs until the
    tenant has explicitly opted into the admin API."""
    async with await _client() as client:
        resp = await client.post(
            "/v1/acl",
            json={
                "document_id": str(acl_scenario["document_id"]),
                "principal_type": "user",
                "principal_id": "user:bob",
                "permission": "read",
            },
            headers=acl_scenario["master_headers"],
        )
    assert resp.status_code == 403


async def test_master_key_can_manage_acl_after_enabling_admin_api(
    acl_scenario: dict[str, object],
) -> None:
    """Once opted in, the master key can create, list, and delete ACL
    entries; a non-admin personal key still cannot."""
    headers = acl_scenario["master_headers"]
    document_id = str(acl_scenario["document_id"])
    async with await _client() as client:
        enable_resp = await client.post("/v1/admin/enable", headers=headers)
        assert enable_resp.status_code == 200

        create_resp = await client.post(
            "/v1/acl",
            json={
                "document_id": document_id,
                "principal_type": "user",
                "principal_id": "user:jane",
                "permission": "read",
            },
            headers=headers,
        )
        assert create_resp.status_code == 200
        entry_id = create_resp.json()["id"]

        list_resp = await client.get(f"/v1/acl/{document_id}", headers=headers)
        assert list_resp.status_code == 200
        assert [e["principal_id"] for e in list_resp.json()] == ["user:jane"]

        personal_list_resp = await client.get(
            f"/v1/acl/{document_id}", headers=acl_scenario["personal_headers"],
        )
        assert personal_list_resp.status_code == 403

        personal_delete_resp = await client.delete(
            f"/v1/acl/{entry_id}", headers=acl_scenario["personal_headers"],
        )
        assert personal_delete_resp.status_code == 403

        delete_resp = await client.delete(f"/v1/acl/{entry_id}", headers=headers)
        assert delete_resp.status_code == 200
