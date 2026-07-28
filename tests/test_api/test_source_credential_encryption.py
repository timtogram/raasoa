"""End-to-end test: connector credentials are actually encrypted at rest
in the database, and correctly decrypted again for sync (F-046 follow-up).

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
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
async def encryption_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "connector_encryption_key", key)
    return key


@pytest.fixture
async def cleanup_source() -> AsyncGenerator[list[uuid.UUID], None]:
    ids: list[uuid.UUID] = []
    yield ids
    if ids:
        from raasoa.db import async_session

        async with async_session() as session:
            await session.execute(
                sql_text("DELETE FROM sync_cursors WHERE source_id = ANY(:ids)"),
                {"ids": ids},
            )
            await session.execute(
                sql_text("DELETE FROM sources WHERE id = ANY(:ids)"), {"ids": ids},
            )
            await session.commit()


async def test_notion_token_is_encrypted_at_rest_and_decrypts_for_sync(
    encryption_key: str, cleanup_source: list[uuid.UUID],
) -> None:
    from raasoa.db import async_session

    real_token = "secret_notion_integration_token_abc123"

    with patch(
        "raasoa.api.sources._sync_notion",
        new=AsyncMock(return_value={"synced": 0, "found": 0, "skipped": 0}),
    ):
        async with await _client() as client:
            resp = await client.post(
                "/v1/sources",
                json={
                    "source_type": "notion",
                    "name": "Encryption Test Source",
                    "config": {"token": real_token},
                    "auto_index": True,
                },
            )
    assert resp.status_code == 200
    source_id = uuid.UUID(resp.json()["id"])
    cleanup_source.append(source_id)

    # The raw DB row must NOT contain the plaintext token.
    async with async_session() as session:
        row = (
            await session.execute(
                sql_text("SELECT connection_config FROM sources WHERE id = :sid"),
                {"sid": source_id},
            )
        ).first()
    assert row is not None
    stored_token = row.connection_config["token"]
    assert stored_token != real_token
    assert stored_token.startswith("enc:v1:")

    # Triggering a manual sync must decrypt it back to the real value
    # before handing it to the connector sync function.
    captured_config: dict[str, object] = {}

    async def _capture_sync(session, tenant_id, source_id, config, query, limit):
        captured_config.update(config)
        return {"synced": 0, "found": 0, "skipped": 0}

    with patch(
        "raasoa.api.sources._sync_notion", new=AsyncMock(side_effect=_capture_sync),
    ):
        async with await _client() as client:
            resp = await client.post(
                f"/v1/sources/{source_id}/sync", json={"query": "*", "limit": 10},
            )
    assert resp.status_code == 200
    assert captured_config.get("token") == real_token


async def test_sharepoint_client_secret_is_encrypted_and_dashboard_sync_decrypts(
    encryption_key: str, cleanup_source: list[uuid.UUID],
) -> None:
    """Same round-trip, through the dashboard's create + sync proxy
    routes rather than the REST API, since those are a separate code
    path that also needed wiring."""
    from raasoa.db import async_session

    real_secret = "az-app-registration-secret-xyz"

    async with await _client() as client:
        resp = await client.post(
            "/dashboard/api/sources",
            json={
                "source_type": "sharepoint",
                "name": "Encryption Test SP Source",
                "config": {
                    "client_secret": real_secret,
                    "site_id": "site-1",
                    "tenant_id_azure": "t", "client_id": "c",
                },
            },
        )
    assert resp.status_code == 200
    source_id = uuid.UUID(resp.json()["id"])
    cleanup_source.append(source_id)

    async with async_session() as session:
        row = (
            await session.execute(
                sql_text("SELECT connection_config FROM sources WHERE id = :sid"),
                {"sid": source_id},
            )
        ).first()
    assert row is not None
    stored_secret = row.connection_config["client_secret"]
    assert stored_secret != real_secret
    assert stored_secret.startswith("enc:v1:")
    # Non-sensitive fields remain plain and readable via JSONB operators.
    assert row.connection_config["site_id"] == "site-1"

    captured_config: dict[str, object] = {}

    async def _capture_sync(session, tenant_id, source_id, config, query, limit):
        captured_config.update(config)
        return {"synced": 0, "found": 0, "skipped": 0, "delta_complete": True}

    with patch(
        "raasoa.api.sources._sync_sharepoint",
        new=AsyncMock(side_effect=_capture_sync),
    ):
        async with await _client() as client:
            resp = await client.post(
                f"/dashboard/api/sources/{source_id}/sync",
                json={"query": "*", "limit": 10},
            )
    assert resp.status_code == 200
    assert captured_config.get("client_secret") == real_secret
