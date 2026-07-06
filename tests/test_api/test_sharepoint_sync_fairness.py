"""Tests for SharePoint multi-drive sync fairness (F-046).

Before this fix, ``_sync_sharepoint``'s delta-sync loop iterated drives in
the SAME fixed order (whatever the Graph API happened to return) on every
call, and broke out as soon as ``stats["synced"] >= limit``. A single busy
first drive that alone consumed the whole limit permanently starved every
other drive — not just once, but forever, since the order never changed.

Cursor persistence was also all-or-nothing: the entire per-drive cursor
map was thrown away unless every drive finished within one call, silently
re-processing already-completed drives' items on every future sync.

Covers two things:
  a. ``_rotate_drives`` — the pure round-robin ordering helper — directly,
     with no mocking.
  b. An end-to-end proof that two consecutive ``_sync_sharepoint`` delta
     calls, with a limit too small for both drives to finish in one call,
     eventually sync BOTH drives instead of the first one forever.

Requires a live Postgres for (b) (cursor persistence goes through
``sync_cursors``). Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sql_text

from raasoa.api.sources import _rotate_drives
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


class TestRotateDrives:
    """Pure function, no mocking or DB needed."""

    def test_no_marker_returns_unchanged(self) -> None:
        drives = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert _rotate_drives(drives, None) == drives

    def test_empty_list_returns_unchanged(self) -> None:
        assert _rotate_drives([], "a") == []

    def test_single_drive_returns_unchanged(self) -> None:
        drives = [{"id": "a"}]
        assert _rotate_drives(drives, "a") == drives

    def test_marker_not_in_list_returns_unchanged(self) -> None:
        drives = [{"id": "a"}, {"id": "b"}]
        assert _rotate_drives(drives, "z") == drives

    def test_rotates_to_start_after_marker_two_drives(self) -> None:
        drives = [{"id": "a"}, {"id": "b"}]
        assert _rotate_drives(drives, "a") == [{"id": "b"}, {"id": "a"}]
        assert _rotate_drives(drives, "b") == [{"id": "a"}, {"id": "b"}]

    def test_rotates_to_start_after_marker_three_drives(self) -> None:
        drives = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert _rotate_drives(drives, "a") == [
            {"id": "b"}, {"id": "c"}, {"id": "a"},
        ]
        assert _rotate_drives(drives, "b") == [
            {"id": "c"}, {"id": "a"}, {"id": "b"},
        ]
        assert _rotate_drives(drives, "c") == [
            {"id": "a"}, {"id": "b"}, {"id": "c"},
        ]

    def test_marker_at_last_position_wraps_to_start(self) -> None:
        drives = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
        assert _rotate_drives(drives, "d") == drives

    def test_full_cycle_visits_every_drive_first_exactly_once(self) -> None:
        """Simulates len(drives) consecutive calls: every drive must be
        first-in-line exactly once before any repeats."""
        drives = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        seen_first: list[str] = []
        marker: str | None = None
        for _ in range(len(drives)):
            ordered = _rotate_drives(drives, marker)
            seen_first.append(str(ordered[0]["id"]))
            marker = str(ordered[0]["id"])
        assert sorted(seen_first) == ["a", "b", "c"]


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
async def source_id() -> AsyncGenerator[uuid.UUID, None]:
    from raasoa.db import async_session
    from raasoa.middleware.auth import DEFAULT_TENANT

    tenant_id = DEFAULT_TENANT
    sid = uuid.uuid4()
    async with async_session() as session:
        result = await session.execute(
            sql_text("SELECT id FROM tenants WHERE id = :tid"), {"tid": tenant_id},
        )
        if not result.first():
            await session.execute(
                sql_text("INSERT INTO tenants (id, name) VALUES (:id, 'Default Tenant')"),
                {"id": tenant_id},
            )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'sharepoint', 'Fairness Test', '{}'::jsonb)"
            ),
            {"id": sid, "tid": tenant_id},
        )
        await session.commit()

    yield sid

    async with async_session() as session:
        await session.execute(
            sql_text(
                "DELETE FROM sync_cursors WHERE source_id = :sid "
                "AND source_type = 'sharepoint'"
            ),
            {"sid": sid},
        )
        await session.execute(
            sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": sid},
        )
        await session.commit()


class _FakeOAuthResponse:
    status_code = 200

    def json(self) -> dict[str, str]:
        return {"access_token": "fake-token"}


class TestSyncSharepointFairnessEndToEnd:
    """Drives the real _sync_sharepoint orchestration (rotation +
    progressive cursor persistence) with the Graph API boundary mocked:
    OAuth token fetch, drive listing, and per-drive delta sync are faked;
    everything else (rotation logic, cursor read/merge/persist via a real
    Postgres) runs for real."""

    async def test_two_calls_eventually_sync_both_drives(
        self, source_id: uuid.UUID,
    ) -> None:
        from raasoa.api.sources import _sync_sharepoint
        from raasoa.db import async_session
        from raasoa.middleware.auth import DEFAULT_TENANT

        drive_a = {"id": "drive-a", "name": "Drive A"}
        drive_b = {"id": "drive-b", "name": "Drive B"}
        # Drive A alone always has more "new" items than the limit below,
        # so under the OLD fixed-order behavior drive B would NEVER get
        # synced across any number of calls.
        limit = 5
        call_log: list[str] = []

        async def _fake_delta_drive(
            *, drive: dict[str, Any], limit: int, stats: dict[str, Any], **kwargs: Any,
        ) -> str | None:
            call_log.append(str(drive["id"]))
            items = min(limit, 10)  # pretend this drive always has 10 new items
            stats["found"] += items
            stats["synced"] += items
            if items >= limit:
                stats["delta_complete"] = False
                return None  # didn't finish this drive
            return f"cursor-for-{drive['id']}"

        with (
            patch(
                "httpx.AsyncClient.post",
                new=AsyncMock(return_value=_FakeOAuthResponse()),
            ),
            patch(
                "raasoa.api.sources._sharepoint_drives",
                new=AsyncMock(return_value=[drive_a, drive_b]),
            ),
            patch(
                "raasoa.api.sources._sync_sharepoint_delta_drive",
                new=_fake_delta_drive,
            ),
        ):
            async with async_session() as session:
                await _sync_sharepoint(
                    session, DEFAULT_TENANT, source_id,
                    {
                        "tenant_id_azure": "t", "client_id": "c",
                        "client_secret": "s", "site_id": "site-1",
                    },
                    query="*", limit=limit,
                )
            async with async_session() as session:
                await _sync_sharepoint(
                    session, DEFAULT_TENANT, source_id,
                    {
                        "tenant_id_azure": "t", "client_id": "c",
                        "client_secret": "s", "site_id": "site-1",
                    },
                    query="*", limit=limit,
                )

        # Old behavior: call_log would be ["drive-a", "drive-a"] forever
        # (drive-b never even attempted, since drive-a alone always hits
        # the limit). New behavior: the second call must start with
        # drive-b instead, since the rotation marker now points past
        # drive-a.
        assert call_log[0] == "drive-a"
        assert call_log[1] == "drive-b"
        assert "drive-b" in call_log, (
            "drive-b was never attempted across two calls -- rotation "
            "fix did not take effect"
        )
