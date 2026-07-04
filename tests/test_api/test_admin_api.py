"""E2E tests for the Admin API (Task #13): groups, memberships, personal
API keys, and source-visibility management — all gated by
tenants.admin_api_enabled and an admin-capable caller.

Requires a live Postgres. Skips gracefully when unreachable.

Uses the app's own global raasoa.db engine throughout, same rationale as
tests/test_api/test_documents_acl.py: resolve_principal_async()'s API-key
lookup always opens its own session from that global engine internally,
so mixing in a private test engine causes cross-loop asyncpg errors.
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


@pytest.fixture
async def admin_scenario() -> AsyncGenerator[dict[str, object], None]:
    """A fresh tenant with AUTH_ENABLED, one legacy master key (DB-issued,
    principal_id=NULL), and admin_api_enabled=false — the true starting
    state of every tenant."""
    import raasoa.config as config_module
    from raasoa.db import async_session

    original_auth_enabled = config_module.settings.auth_enabled
    config_module.settings.auth_enabled = True

    tenant_id = uuid.uuid4()
    raw_master_key = "sk-master-" + secrets.token_hex(16)
    master_key_hash = hashlib.sha256(raw_master_key.encode()).hexdigest()

    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'AdminApiTest')"),
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
        await session.commit()

    yield {
        "tenant_id": tenant_id,
        "master_headers": {"Authorization": f"Bearer {raw_master_key}"},
    }

    config_module.settings.auth_enabled = original_auth_enabled
    async with async_session() as session:
        await session.execute(
            sql_text("DELETE FROM principal_memberships WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM principal_groups WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        await session.execute(
            sql_text("DELETE FROM source_acl_grants WHERE tenant_id = :tid"),
            {"tid": tenant_id},
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


async def test_admin_endpoints_403_before_enable(
    admin_scenario: dict[str, object],
) -> None:
    """Even the master key can't use admin endpoints until it explicitly
    opts the tenant in via POST /v1/admin/enable."""
    headers = admin_scenario["master_headers"]
    async with await _client() as client:
        resp = await client.get("/v1/admin/groups", headers=headers)
    assert resp.status_code == 403


async def test_non_master_cannot_enable_admin_api(
    admin_scenario: dict[str, object],
) -> None:
    """A personal (non-legacy) key — even one with is_admin=true set
    manually — cannot be the one to flip admin_api_enabled on; only the
    tenant's own legacy master key can."""
    import raasoa.config as config_module
    from raasoa.db import async_session

    tenant_id = admin_scenario["tenant_id"]
    raw_key = "sk-personal-" + secrets.token_hex(16)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with async_session() as session:
        await session.execute(
            sql_text(
                "INSERT INTO api_keys "
                "(id, tenant_id, key_hash, key_prefix, name, principal_id, "
                " is_admin, is_active) "
                "VALUES (:id, :tid, :hash, :prefix, 'FakeAdmin', 'user:eve', true, true)"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "hash": key_hash, "prefix": raw_key[:10]},
        )
        await session.commit()

    assert config_module.settings.auth_enabled is True
    async with await _client() as client:
        resp = await client.post(
            "/v1/admin/enable", headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert resp.status_code == 403


async def test_full_admin_lifecycle(admin_scenario: dict[str, object]) -> None:
    """Enable -> create group -> add member -> issue personal key ->
    personal key can list groups -> effective-access reflects the grant."""
    headers = admin_scenario["master_headers"]
    async with await _client() as client:
        enable_resp = await client.post("/v1/admin/enable", headers=headers)
        assert enable_resp.status_code == 200

        status_resp = await client.get("/v1/admin/status", headers=headers)
        assert status_resp.json()["admin_api_enabled"] is True

        group_resp = await client.post(
            "/v1/admin/groups",
            json={"principal_id": "group:sales", "display_name": "Sales Team"},
            headers=headers,
        )
        assert group_resp.status_code == 200
        assert group_resp.json()["principal_id"] == "group:sales"

        dup_resp = await client.post(
            "/v1/admin/groups",
            json={"principal_id": "group:sales"},
            headers=headers,
        )
        assert dup_resp.status_code == 409

        member_resp = await client.post(
            "/v1/admin/groups/group:sales/members",
            json={"member_principal_id": "user:jane"},
            headers=headers,
        )
        assert member_resp.status_code == 200

        members_resp = await client.get(
            "/v1/admin/groups/group:sales/members", headers=headers,
        )
        assert [m["member_principal_id"] for m in members_resp.json()] == ["user:jane"]

        key_resp = await client.post(
            "/v1/admin/keys",
            json={"name": "Jane", "principal_id": "user:jane", "clearance": "internal"},
            headers=headers,
        )
        assert key_resp.status_code == 200
        jane_key = key_resp.json()["key"]
        assert key_resp.json()["principal_id"] == "user:jane"

        keys_list_resp = await client.get("/v1/admin/keys", headers=headers)
        principal_ids = {k["principal_id"] for k in keys_list_resp.json()}
        assert "user:jane" in principal_ids

        # Jane's own personal key is NOT admin — must not be able to use
        # the admin API herself.
        jane_admin_attempt = await client.get(
            "/v1/admin/groups", headers={"Authorization": f"Bearer {jane_key}"},
        )
        assert jane_admin_attempt.status_code == 403

        source_resp = await client.post(
            "/v1/sources",
            json={"source_type": "notion", "name": "Sales Wiki", "auto_index": False},
            headers=headers,
        )
        assert source_resp.status_code == 200
        assert source_resp.json()["default_visibility"] == "inherit"
        source_id = source_resp.json()["id"]

        visibility_resp = await client.patch(
            f"/v1/sources/{source_id}/visibility",
            json={"default_visibility": "restricted", "grant_principal_ids": ["group:sales"]},
            headers=headers,
        )
        assert visibility_resp.status_code == 200

        access_resp = await client.get(
            "/v1/admin/effective-access",
            params={"principal_id": "user:jane"},
            headers=headers,
        )
        assert access_resp.status_code == 200
        access_body = access_resp.json()
        assert "group:sales" in access_body["resolved_principal_ids"]
        sales_wiki = next(
            s for s in access_body["sources"] if s["source_id"] == source_id
        )
        assert sales_wiki["visible"] is True
        assert sales_wiki["via"] == "source_acl_grant"

        remove_resp = await client.delete(
            "/v1/admin/groups/group:sales/members/user:jane", headers=headers,
        )
        assert remove_resp.status_code == 200

        access_resp_2 = await client.get(
            "/v1/admin/effective-access",
            params={"principal_id": "user:jane"},
            headers=headers,
        )
        sales_wiki_2 = next(
            s for s in access_resp_2.json()["sources"] if s["source_id"] == source_id
        )
        assert sales_wiki_2["visible"] is False

        delete_group_resp = await client.delete(
            "/v1/admin/groups/group:sales", headers=headers,
        )
        assert delete_group_resp.status_code == 200


async def test_hubspot_source_defaults_to_restricted(
    admin_scenario: dict[str, object],
) -> None:
    headers = admin_scenario["master_headers"]
    async with await _client() as client:
        resp = await client.post(
            "/v1/sources",
            json={"source_type": "hubspot", "name": "CRM", "auto_index": False},
            headers=headers,
        )
    assert resp.status_code == 200
    assert resp.json()["default_visibility"] == "restricted"


async def test_notion_source_defaults_to_inherit(
    admin_scenario: dict[str, object],
) -> None:
    headers = admin_scenario["master_headers"]
    async with await _client() as client:
        resp = await client.post(
            "/v1/sources",
            json={"source_type": "notion", "name": "Wiki", "auto_index": False},
            headers=headers,
        )
    assert resp.status_code == 200
    assert resp.json()["default_visibility"] == "inherit"
