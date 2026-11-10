from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


def supervised(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    restart: bool = True,
    min_backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> asyncio.Task:
    """Run a long-lived coroutine, log failures and restart it with exponential backoff."""

    async def runner() -> None:
        backoff = min_backoff
        while True:
            try:
                await factory()
                if not restart:
                    return
                log.warning("Task %s exited; restarting", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Task %s crashed", name)
                if not restart:
                    return
            await asyncio.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)

    return asyncio.create_task(runner(), name=name)


async def cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
