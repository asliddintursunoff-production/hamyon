"""Telethon client lifecycle for the owner's personal account (read HUMO messages, request balances)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.config import Settings
from app.database.session import SessionFactory
from app.telegram.balance_sync import BalanceSyncCoordinator
from app.telegram.history_sync import sync_history
from app.telegram.listener import HumoMessageHandler
from app.utils.tasks import cancel_tasks, supervised

log = logging.getLogger(__name__)


def build_client(settings: Settings):
    from telethon import TelegramClient

    if settings.telethon_session:
        from telethon.sessions import StringSession

        session = StringSession(settings.telethon_session.get_secret_value().strip())
    else:
        path = Path(settings.telethon_session_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        session = str(path)
    return TelegramClient(
        session,
        settings.telethon_api_id,
        settings.telethon_api_hash.get_secret_value(),
        connection_retries=None,  # keep retrying
        retry_delay=5,
        auto_reconnect=True,
        catch_up=True,
    )


class TelethonService:
    def __init__(self, settings: Settings, sf: SessionFactory, handler: HumoMessageHandler, balance: BalanceSyncCoordinator):
        self.settings = settings
        self.sf = sf
        self.handler = handler
        self.balance = balance
        self.client = None
        self.entity = None
        self.me_id: int | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected() and self.entity is not None

    async def start(self) -> bool:
        if not self.settings.telethon_configured:
            log.warning("Telethon not configured (TELETHON_API_ID/HASH); HUMO ingestion disabled")
            return False
        from telethon import events

        self.client = build_client(self.settings)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.error("Telethon session is not authorized. Run `python -m app.telegram.login` once to log in.")
            await self.client.disconnect()
            self.client = None
            return False

        me = await self.client.get_me()
        self.me_id = me.id
        self.entity = await self.client.get_input_entity(self.settings.humo_bot_username)
        humo = await self.client.get_entity(self.entity)
        if not getattr(humo, "bot", False):
            log.error("%s is not a bot account; refusing to ingest from it", self.settings.humo_bot_username)
            await self.client.disconnect()
            self.client = None
            return False
        self.handler.humo_chat_id = humo.id
        self.balance.attach(self.client, self.entity)

        async def on_new(event):
            await self._safe_handle(event.message, edited=False)

        async def on_edit(event):
            await self._safe_handle(event.message, edited=True)

        self.client.add_event_handler(on_new, events.NewMessage(chats=[self.entity], incoming=True))
        self.client.add_event_handler(on_edit, events.MessageEdited(chats=[self.entity], incoming=True))

        log.info("Telethon connected; listening to @%s", self.settings.humo_bot_username)
        self._tasks.append(supervised("telethon-connection", self._keep_connected))
        self._tasks.append(supervised("humo-history-sync", self._periodic_sync))
        return True

    async def _safe_handle(self, msg, *, edited: bool) -> None:
        try:
            await self.handler.handle(msg, edited=edited)
        except Exception:
            # The message is still in Telegram; the periodic history sync will pick it up again.
            log.exception("failed to ingest HUMO message %s", getattr(msg, "id", "?"))

    async def _keep_connected(self) -> None:
        while not self._stopping:
            if not self.client.is_connected():
                log.warning("Telethon disconnected; reconnecting")
                await self.client.connect()
                await self.sync_now()
            await self.client.disconnected
            if self._stopping:
                return
            await asyncio.sleep(5)

    async def _periodic_sync(self) -> None:
        while not self._stopping:
            await self.sync_now()
            await asyncio.sleep(self.settings.history_sync_interval_seconds)

    async def sync_now(self) -> None:
        try:
            await sync_history(self.client, self.entity, self.handler, self.sf, self.settings.history_sync_initial_limit)
        except Exception:
            log.exception("HUMO history sync failed")

    async def stop(self) -> None:
        self._stopping = True
        await cancel_tasks(self._tasks)
        self._tasks.clear()
        if self.client is not None:
            await self.client.disconnect()
