"""Scheduled source sync — polls connectors on configured intervals.

Checks all sources with a sync_interval set and triggers sync
when the interval has elapsed since the last sync.

Usage:
    # Run as background worker alongside the API
    uv run python -m raasoa.worker.sync_scheduler

    # Or integrate into the main worker loop
    from raasoa.worker.sync_scheduler import run_scheduled_syncs
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text

from raasoa.db import async_session

logger = logging.getLogger(__name__)


async def run_scheduled_syncs() -> dict[str, Any]:
    """Check all sources and sync those whose interval has elapsed."""
    stats: dict[str, Any] = {"checked": 0, "synced": 0, "errors": []}

    async with async_session() as session:
        # Find sources due for sync
        result = await session.execute(
            text(
                "SELECT s.id, s.tenant_id, s.source_type, s.name, "
                "  s.connection_config, "
                "  (s.connection_config->>'sync_interval_minutes')::int "
                "    AS interval_min, "
                "  sc.last_sync_at "
                "FROM sources s "
                "LEFT JOIN sync_cursors sc "
                "  ON sc.source_id = s.id "
                "  AND sc.source_type = s.source_type "
                "WHERE s.connection_config->>'sync_interval_minutes' "
                "  IS NOT NULL "
                "AND ("
                "  sc.last_sync_at IS NULL "
                "  OR sc.last_sync_at < now() - "
                "    ((s.connection_config"
                "      ->>'sync_interval_minutes')::int "
                "      || ' minutes')::interval"
                ") "
                "AND (sc.sync_status IS NULL "
                "  OR sc.sync_status != 'running')"
            )
        )
        due_sources = result.fetchall()
        stats["checked"] = len(due_sources)

        for source in due_sources:
            logger.info(
                "Scheduled sync: %s (%s) — interval %d min",
                source.name, source.source_type, source.interval_min,
            )
            try:
                # Reuse the sync logic from the sources API
                from raasoa.api.sources import (
                    _sync_jira,
                    _sync_notion,
                    _sync_sharepoint,
                )

                config = source.connection_config or {}

                if source.source_type == "notion":
                    sync_stats = await _sync_notion(
                        session,
                        source.tenant_id,
                        source.id,
                        config,
                        query="*",
                        limit=100,
                    )
                    stats["synced"] += sync_stats.get("synced", 0)
                    logger.info(
                        "Sync complete: %s — %d synced",
                        source.name, sync_stats.get("synced", 0),
                    )
                elif source.source_type == "sharepoint":
                    sync_stats = await _sync_sharepoint(
                        session,
                        source.tenant_id,
                        source.id,
                        config,
                        query="*",
                        limit=500,
                    )
                    stats["synced"] += sync_stats.get("synced", 0)
                    logger.info(
                        "Sync complete: %s — %d synced",
                        source.name, sync_stats.get("synced", 0),
                    )
                elif source.source_type == "jira":
                    sync_stats = await _sync_jira(
                        session,
                        source.tenant_id,
                        source.id,
                        config,
                        query="*",
                        limit=500,
                    )
                    stats["synced"] += sync_stats.get("synced", 0)
                    logger.info(
                        "Sync complete: %s — %d synced",
                        source.name, sync_stats.get("synced", 0),
                    )
                else:
                    sync_stats = {"synced": 0}
                    logger.debug(
                        "Skipping %s — no auto-sync for %s",
                        source.name, source.source_type,
                    )

                # Update sync cursor
                await session.execute(
                    text(
                        "INSERT INTO sync_cursors "
                        "(source_type, source_id, sync_status, "
                        " last_sync_at, items_synced) "
                        "VALUES (:stype, :sid, 'completed', "
                        " now(), :count) "
                        "ON CONFLICT (source_type, source_id) "
                        "DO UPDATE SET sync_status = 'completed', "
                        "  last_sync_at = now(), "
                        "  items_synced = :count"
                    ),
                    {
                        "stype": source.source_type,
                        "sid": source.id,
                        "count": sync_stats.get("synced", 0),
                    },
                )
                await session.commit()

            except Exception as e:
                logger.exception("Scheduled sync failed: %s", source.name)
                stats["errors"].append({
                    "source": source.name, "error": str(e)[:200],
                })
                await session.rollback()

    return stats


async def scheduler_loop(check_interval: float = 60.0) -> None:
    """Run the sync scheduler in a loop.

    Checks every `check_interval` seconds for sources due for sync.
    """
    logger.info(
        "Sync scheduler started (check every %.0fs)", check_interval,
    )
    while True:
        try:
            stats = await run_scheduled_syncs()
            if stats["synced"] > 0:
                logger.info("Scheduled syncs: %s", stats)
        except Exception:
            logger.exception("Scheduler loop error")
        await asyncio.sleep(check_interval)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    await scheduler_loop()


if __name__ == "__main__":
    asyncio.run(main())
