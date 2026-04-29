"""Production scheduler — runs all maintenance loops in one process.

Schedules:
  - source sync (every 60s; per-source interval respected)
  - job queue drain (every 30s)
  - retention cleanup (every 6h)
  - tiering sweep (every 24h)

Usage:
    uv run python -m raasoa.worker.scheduler
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from raasoa.worker.queue import process_one
from raasoa.worker.retention import run_retention_cleanup
from raasoa.worker.sync_scheduler import run_scheduled_syncs


async def _drain_queue() -> dict[str, int]:
    """Drain up to N jobs per tick so we don't starve the loop."""
    processed = 0
    for _ in range(20):
        if not await process_one():
            break
        processed += 1
    return {"processed": processed}

logger = logging.getLogger(__name__)

_SHUTDOWN = asyncio.Event()


def _install_signal_handlers() -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _SHUTDOWN.set)


async def _run_loop(
    name: str,
    interval_s: int,
    fn: Callable[[], Awaitable[Any]],
    *,
    jitter_s: int = 5,
) -> None:
    """Run ``fn()`` every ``interval_s`` until shutdown. Errors are logged but
    don't kill the loop."""
    logger.info("scheduler[%s] starting (interval=%ss)", name, interval_s)
    while not _SHUTDOWN.is_set():
        try:
            t0 = datetime.now(UTC)
            result = await fn()
            elapsed = (datetime.now(UTC) - t0).total_seconds()
            logger.info(
                "scheduler[%s] tick ok elapsed=%.2fs result=%s",
                name, elapsed, result,
            )
        except Exception:
            logger.exception("scheduler[%s] tick failed", name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                _SHUTDOWN.wait(), timeout=interval_s + jitter_s,
            )
    logger.info("scheduler[%s] stopped", name)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    _install_signal_handlers()

    sync_interval = int(os.environ.get("SCHEDULER_SYNC_INTERVAL", "60"))
    queue_interval = int(os.environ.get("SCHEDULER_QUEUE_INTERVAL", "30"))
    retention_interval = int(
        os.environ.get("SCHEDULER_RETENTION_INTERVAL", str(6 * 3600)),
    )

    logger.info("RAASOA scheduler starting…")

    tasks = [
        asyncio.create_task(_run_loop("sync", sync_interval, run_scheduled_syncs)),
        asyncio.create_task(_run_loop("queue", queue_interval, _drain_queue)),
        asyncio.create_task(
            _run_loop("retention", retention_interval, run_retention_cleanup),
        ),
    ]
    try:
        await _SHUTDOWN.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("RAASOA scheduler exited cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
