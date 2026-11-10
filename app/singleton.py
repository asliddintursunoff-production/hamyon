"""Only one running instance may use the Telegram session and the bot token.

Hosts like Railway start the new deployment before stopping the old one. Two Telethon clients on the
same login can get the session revoked by Telegram, and two bot pollers conflict. The instance that
holds a session-level PostgreSQL advisory lock runs the Telegram side; a new instance serves HTTP
(health checks pass) and waits for the lock, which is released when the old process exits.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)

INSTANCE_LOCK = 7_202_613


class InstanceLock:
    def __init__(self, engine: AsyncEngine, poll_seconds: float = 3.0):
        self.engine = engine
        self.poll_seconds = poll_seconds
        self._conn: AsyncConnection | None = None

    async def acquire(self) -> None:
        waiting_logged = False
        while True:
            conn = await self.engine.connect()
            try:
                got = (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": INSTANCE_LOCK})).scalar()
                await conn.commit()  # end the implicit transaction; the session-level lock stays
            except BaseException:
                await conn.close()
                raise
            if got:
                self._conn = conn
                log.info("instance lock acquired")
                return
            await conn.close()
            if not waiting_logged:
                log.info("another instance is running; waiting for it to stop")
                waiting_logged = True
            await asyncio.sleep(self.poll_seconds)

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("select pg_advisory_unlock(:k)"), {"k": INSTANCE_LOCK})
            await self._conn.commit()
        except Exception:
            pass  # closing the connection releases it anyway
        finally:
            await self._conn.close()
            self._conn = None
