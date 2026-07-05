"""Regression tests for the create_source admin-gating + SSRF fix (F-011).

Before this fix, POST /v1/sources only resolved the caller's tenant —
any valid tenant key, including a non-admin personal key, could create
a source (e.g. Jira) whose connection_config.base_url points the
server's outbound sync requests at an arbitrary host (cloud metadata
endpoints, internal services). Now:
  - creation requires an admin-capable caller (legacy/master key or a
    personal key with is_admin=true) — but NOT the extra
    tenants.admin_api_enabled opt-in, since that flag governs the
    delegated Admin API, not this basic tenant operation;
  - a Jira base_url is validated against the SSRF guard at creation time.

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
async def source_gate_scenario() -> AsyncGenerator[dict[str, object], None]:
    """A fresh tenant with AUTH_ENABLED, a legacy master key (no admin
    API opt-in), and a non-admin personal key."""
    import raasoa.config as config_module
    from raasoa.db import async_session

    original_auth_enabled = config_module.settings.auth_enabled
    config_module.settings.auth_enabled = True

    tenant_id = uuid.uuid4()
    raw_master_key = "sk-master-" + secrets.token_hex(16)
    master_key_hash = hashlib.sha256(raw_master_key.encode()).hexdigest()
    raw_personal_key = "sk-personal-" + secrets.token_hex(16)
    personal_key_hash = hashlib.sha256(raw_personal_key.encode()).hexdigest()

    async with async_session() as session:
        await session.execute(
            sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'SourceGateTest')"),
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
        await session.commit()

    yield {
        "tenant_id": tenant_id,
        "master_headers": {"Authorization": f"Bearer {raw_master_key}"},
        "personal_headers": {"Authorization": f"Bearer {raw_personal_key}"},
    }

    config_module.settings.auth_enabled = original_auth_enabled
    async with async_session() as session:
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


async def test_personal_key_cannot_create_source(
    source_gate_scenario: dict[str, object],
) -> None:
    """A non-admin personal key must not be able to create sources at
    all — closes off the SSRF vector at the source."""
    async with await _client() as client:
        resp = await client.post(
            "/v1/sources",
            json={"source_type": "notion", "name": "Wiki", "auto_index": False},
            headers=source_gate_scenario["personal_headers"],
        )
    assert resp.status_code == 403


async def test_master_key_can_create_source_without_admin_api_opt_in(
    source_gate_scenario: dict[str, object],
) -> None:
    """Regression guard: the tenant's own master key must keep working
    without first calling POST /v1/admin/enable — that flag is for the
    delegated Admin API, not basic source creation."""
    async with await _client() as client:
        resp = await client.post(
            "/v1/sources",
            json={"source_type": "notion", "name": "Wiki", "auto_index": False},
            headers=source_gate_scenario["master_headers"],
        )
    assert resp.status_code == 200


async def test_jira_source_with_ssrf_target_rejected(
    source_gate_scenario: dict[str, object],
) -> None:
    """The exact escalation from F-011: a Jira base_url pointed at the
    cloud metadata endpoint must be rejected at creation time."""
    async with await _client() as client:
        resp = await client.post(
            "/v1/sources",
            json={
                "source_type": "jira",
                "name": "Evil Jira",
                "auto_index": False,
                "config": {
                    "base_url": "https://169.254.169.254",
                    "email": "a@b.com",
                    "api_token": "x",
                },
            },
            headers=source_gate_scenario["master_headers"],
        )
    assert resp.status_code == 400


async def test_jira_source_with_valid_url_accepted(
    source_gate_scenario: dict[str, object],
) -> None:
    async with await _client() as client:
        resp = await client.post(
            "/v1/sources",
            json={
                "source_type": "jira",
                "name": "Real Jira",
                "auto_index": False,
                "config": {
                    "base_url": "https://example.atlassian.net",
                    "email": "a@b.com",
                    "api_token": "x",
                },
            },
            headers=source_gate_scenario["master_headers"],
        )
    assert resp.status_code == 200
