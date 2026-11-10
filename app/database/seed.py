from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cards.service import ensure_card
from app.config import Settings
from app.database.session import SessionFactory
from app.models import User
from app.transactions.categories import seed_categories


async def get_owner(session: AsyncSession) -> User:
    owner = (await session.scalars(select(User).order_by(User.id).limit(1))).first()
    if owner is None:
        raise RuntimeError("owner record missing; application not seeded")
    return owner


async def seed(session_factory: SessionFactory, settings: Settings) -> int:
    """Idempotent startup seed: owner record, categories, initial cards. Returns owner id."""
    async with session_factory.begin() as s:
        # Serialize concurrent seeders (e.g. tests) with a transaction-level advisory lock.
        await s.execute(select(func.pg_advisory_xact_lock(7_202_611)))
        owner = (await s.scalars(select(User).order_by(User.id).limit(1))).first()
        if owner is None:
            owner = User(
                telegram_id=settings.owner_telegram_id,
                timezone=settings.timezone,
                currency=settings.currency,
            )
            s.add(owner)
            await s.flush()
        elif settings.owner_telegram_id and owner.telegram_id != settings.owner_telegram_id:
            owner.telegram_id = settings.owner_telegram_id
        await seed_categories(s)
        for last4, bank in settings.parsed_initial_cards():
            await ensure_card(s, owner.id, last4, bank_name=bank, currency=settings.currency)
        return owner.id


async def set_owner_telegram_id(session_factory: SessionFactory, owner_id: int, telegram_id: int) -> None:
    async with session_factory.begin() as s:
        await s.execute(update(User).where(User.id == owner_id).values(telegram_id=telegram_id))
