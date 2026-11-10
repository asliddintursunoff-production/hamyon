"""Collect HUMO messages from the chat history.

- First start (empty database): the whole HUMO chat history is imported, oldest first.
- Every start: messages newer than the newest stored one are recovered (received while offline),
  the newest messages are re-checked, and any history OLDER than the oldest stored message is
  back-filled. So a database that was started with a date limit fills itself up to full history.

Every message is ingested idempotently (unique chat_id + message_id), so overlapping with the live
listener or re-scanning never creates duplicates.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.database.session import SessionFactory
from app.models import TelegramMessage
from app.telegram.listener import HumoMessageHandler

log = logging.getLogger(__name__)

RECENT_RESCAN = 50  # always re-check the newest messages, in case one failed to persist earlier


async def stored_id_range(sf: SessionFactory, chat_id: int) -> tuple[int, int]:
    async with sf() as s:
        row = (
            await s.execute(
                select(func.min(TelegramMessage.telegram_message_id), func.max(TelegramMessage.telegram_message_id))
                .where(TelegramMessage.chat_id == chat_id)
            )
        ).one()
        return row[0] or 0, row[1] or 0


async def last_known_message_id(sf: SessionFactory, chat_id: int) -> int:
    return (await stored_id_range(sf, chat_id))[1]


async def sync_history(client, entity, handler: HumoMessageHandler, sf: SessionFactory, initial_limit: int | None) -> int:
    chat_id = handler.humo_chat_id
    start = handler.settings.history_start
    oldest, newest = await stored_id_range(sf, chat_id)
    count = 0

    if not newest:
        # First start: everything (or everything since the optional start date), oldest first.
        kwargs = {"offset_date": start} if start is not None else {}
        async for msg in client.iter_messages(entity, reverse=True, limit=initial_limit, **kwargs):
            await handler.handle(msg)
            count += 1
        log.info("HUMO initial import: %s message(s)", count)
        return count

    # Messages that arrived while the app was offline.
    async for msg in client.iter_messages(entity, min_id=newest, reverse=True):
        await handler.handle(msg)
        count += 1
    for msg in reversed(list(await client.get_messages(entity, limit=RECENT_RESCAN))):
        await handler.handle(msg)

    # Back-fill older history not stored yet (no-op once the beginning of the chat is reached).
    older = []
    async for msg in client.iter_messages(entity, max_id=oldest):
        if start is not None and msg.date < start:
            break
        older.append(msg)
    for msg in reversed(older):
        await handler.handle(msg)

    log.info("HUMO history sync: %s new message(s), %s older message(s) back-filled", count, len(older))
    return count + len(older)
