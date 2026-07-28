"""Tests for the scheduled-sync status bug (F-046 follow-up).

``run_scheduled_syncs`` used to unconditionally mark a source
"completed" after every scheduled call, regardless of whether the
connector itself reported ``delta_complete=False`` (i.e. its backlog
didn't fit in one call's limit -- the common case for a large SharePoint
library). This masked the problem from operators (dashboard always shows
"completed") and, since a source stuck reprocessing the same backlog
never looked any different from one that's genuinely caught up, there
was no way to tell a real convergence problem from normal steady-state
operation.

The fix introduces a third status, "incomplete", and makes the due-query
treat it as due immediately (ignoring the normal interval) so a large
initial sync converges over consecutive scheduler ticks instead of one
batch per interval.

Requires a live Postgres. Skips gracefully when unreachable.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sql_text

from raasoa.config import settings
from raasoa.worker.sync_scheduler import run_scheduled_syncs

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
async def source_id() -> AsyncGenerator[uuid.UUID, None]:
    from raasoa.db import async_session
    from raasoa.middleware.auth import DEFAULT_TENANT

    sid = uuid.uuid4()
    async with async_session() as session:
        await session.execute(
            sql_text("SELECT 1 FROM tenants WHERE id = :tid"), {"tid": DEFAULT_TENANT},
        )
        await session.execute(
            sql_text(
                "INSERT INTO sources "
                "(id, tenant_id, source_type, name, connection_config) "
                "VALUES (:id, :tid, 'sharepoint', 'Test SharePoint', "
                " '{\"sync_interval_minutes\": 60}'::jsonb)"
            ),
            {"id": sid, "tid": DEFAULT_TENANT},
        )
        await session.commit()

    yield sid

    async with async_session() as session:
        await session.execute(
            sql_text("DELETE FROM sync_cursors WHERE source_id = :sid"), {"sid": sid},
        )
        await session.execute(
            sql_text("DELETE FROM sources WHERE id = :sid"), {"sid": sid},
        )
        await session.commit()


class TestScheduledSyncStatusReflectsCompletion:
    async def test_incomplete_sync_is_marked_incomplete_not_completed(
        self, source_id: uuid.UUID,
    ) -> None:
        """Regression: this used to always write 'completed' regardless
        of delta_complete."""
        from raasoa.db import async_session

        with patch(
            "raasoa.api.sources._sync_sharepoint",
            new=AsyncMock(return_value={"synced": 3, "delta_complete": False}),
        ):
            await run_scheduled_syncs()

        async with async_session() as session:
            row = (
                await session.execute(
                    sql_text(
                        "SELECT sync_status FROM sync_cursors "
                        "WHERE source_id = :sid AND source_type = 'sharepoint'"
                    ),
                    {"sid": source_id},
                )
            ).first()
        assert row is not None
        assert row.sync_status == "incomplete"

    async def test_complete_sync_is_marked_completed(
        self, source_id: uuid.UUID,
    ) -> None:
        from raasoa.db import async_session

        with patch(
            "raasoa.api.sources._sync_sharepoint",
            new=AsyncMock(return_value={"synced": 5, "delta_complete": True}),
        ):
            await run_scheduled_syncs()

        async with async_session() as session:
            row = (
                await session.execute(
                    sql_text(
                        "SELECT sync_status FROM sync_cursors "
                        "WHERE source_id = :sid AND source_type = 'sharepoint'"
                    ),
                    {"sid": source_id},
                )
            ).first()
        assert row is not None
        assert row.sync_status == "completed"

    async def test_missing_delta_complete_key_defaults_to_completed(
        self, source_id: uuid.UUID,
    ) -> None:
        """Connectors that don't report delta_complete at all (e.g. Notion,
        Jira today) must keep their prior behavior unchanged."""
        from raasoa.db import async_session

        with patch(
            "raasoa.api.sources._sync_sharepoint",
            new=AsyncMock(return_value={"synced": 2}),
        ):
            await run_scheduled_syncs()

        async with async_session() as session:
            row = (
                await session.execute(
                    sql_text(
                        "SELECT sync_status FROM sync_cursors "
                        "WHERE source_id = :sid AND source_type = 'sharepoint'"
                    ),
                    {"sid": source_id},
                )
            ).first()
        assert row is not None
        assert row.sync_status == "completed"


class TestIncompleteSourcesAreDueImmediately:
    async def test_incomplete_status_bypasses_the_interval_wait(
        self, source_id: uuid.UUID,
    ) -> None:
        """A source marked 'incomplete' just moments ago (last_sync_at
        far short of its 60-minute interval) must still be picked up on
        the very next tick -- known pending backlog shouldn't wait a
        full interval to make more progress."""
        from raasoa.db import async_session

        async with async_session() as session:
            await session.execute(
                sql_text(
                    "INSERT INTO sync_cursors "
                    "(source_type, source_id, sync_status, last_sync_at) "
                    "VALUES ('sharepoint', :sid, 'incomplete', now())"
                ),
                {"sid": source_id},
            )
            await session.commit()

        mock_sync = AsyncMock(return_value={"synced": 0, "delta_complete": True})
        with patch("raasoa.api.sources._sync_sharepoint", new=mock_sync):
            stats = await run_scheduled_syncs()

        mock_sync.assert_called_once()
        assert stats["checked"] == 1

    async def test_completed_status_respects_the_interval_wait(
        self, source_id: uuid.UUID,
    ) -> None:
        """A source marked 'completed' moments ago must NOT be re-synced
        again before its interval elapses -- unaffected baseline
        behavior."""
        from raasoa.db import async_session

        async with async_session() as session:
            await session.execute(
                sql_text(
                    "INSERT INTO sync_cursors "
                    "(source_type, source_id, sync_status, last_sync_at) "
                    "VALUES ('sharepoint', :sid, 'completed', now())"
                ),
                {"sid": source_id},
            )
            await session.commit()

        mock_sync = AsyncMock(return_value={"synced": 0, "delta_complete": True})
        with patch("raasoa.api.sources._sync_sharepoint", new=mock_sync):
            stats = await run_scheduled_syncs()

        mock_sync.assert_not_called()
        assert stats["checked"] == 0

    async def test_running_status_is_never_due_regardless_of_incomplete_check(
        self, source_id: uuid.UUID,
    ) -> None:
        """'running' means a call is actively in flight right now -- must
        never be picked up concurrently, even though it's neither
        'completed' nor past its interval."""
        from raasoa.db import async_session

        async with async_session() as session:
            await session.execute(
                sql_text(
                    "INSERT INTO sync_cursors "
                    "(source_type, source_id, sync_status, last_sync_at) "
                    "VALUES ('sharepoint', :sid, 'running', now())"
                ),
                {"sid": source_id},
            )
            await session.commit()

        mock_sync = AsyncMock(return_value={"synced": 0, "delta_complete": True})
        with patch("raasoa.api.sources._sync_sharepoint", new=mock_sync):
            stats = await run_scheduled_syncs()

        mock_sync.assert_not_called()
        assert stats["checked"] == 0
