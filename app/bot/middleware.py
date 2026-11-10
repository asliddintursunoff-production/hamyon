from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

log = logging.getLogger(__name__)


class PrivateChatOnlyMiddleware(BaseMiddleware):
    """Outer update middleware: only private chats with real users. Access itself is granted per user
    by entering BOT_ACCESS_CODE (see RequireUnlockedMiddleware)."""

    def __init__(self) -> None:
        self._last_warned: dict[int, float] = {}

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: Update, data: dict[str, Any]
    ) -> Any:
        ctx = data["ctx"]
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None:
            return None
        if user.is_bot or (chat is not None and chat.type != "private"):
            log.warning("rejected Telegram update from user %s in %s chat", user.id, getattr(chat, "type", "?"))
            # Answer at most once a minute per stranger to avoid being used for spam.
            now = time.monotonic()
            if now - self._last_warned.get(user.id, 0) > 60:
                self._last_warned[user.id] = now
                try:
                    if event.message:
                        await event.message.answer("⛔ This bot only works in a private chat.")
                    elif event.callback_query:
                        await event.callback_query.answer("⛔ Access denied", show_alert=True)
                except Exception:
                    pass
            return None
        return await handler(event, data)


class RequireUnlockedMiddleware(BaseMiddleware):
    """Router-level middleware: the owner must have entered the access code."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx = data["ctx"]
        user = data.get("event_from_user")
        if user is not None and await ctx.auth.is_authenticated(user.id):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("🔒 The bot is locked. Send /start and enter your access code.")
        elif isinstance(event, CallbackQuery):
            await event.answer("🔒 Locked. Send /start to unlock.", show_alert=True)
        return None
