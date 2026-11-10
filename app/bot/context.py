from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.assistant.service import Assistant
from app.bot.services.auth import BotAuthService
from app.config import Settings
from app.database.session import SessionFactory
from app.models import User
from app.telegram.balance_sync import BalanceSyncCoordinator
from app.transactions.pipeline import MessageProcessor


@dataclass
class BotContext:
    settings: Settings
    sf: SessionFactory
    auth: BotAuthService
    assistant: Assistant
    processor: MessageProcessor
    balance: BalanceSyncCoordinator
    owner_db_id: int
    owner_telegram_id: int | None = None

    async def owner(self) -> User:
        async with self.sf() as s:
            return await s.get(User, self.owner_db_id)

    async def tz(self) -> ZoneInfo:
        owner = await self.owner()
        try:
            return ZoneInfo(owner.timezone)
        except Exception:
            return self.settings.tz
