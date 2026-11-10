"""Cards and balance snapshots. Balances are only ever set from bank-reported values."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BalanceSnapshot, Card


async def ensure_card(
    session: AsyncSession, owner_id: int, last4: str, *, bank_name: str | None = None, currency: str = "UZS"
) -> Card:
    await session.execute(
        insert(Card)
        .values(owner_id=owner_id, card_last_four=last4, bank_name=bank_name, currency=currency)
        .on_conflict_do_nothing(constraint="uq_cards_owner_last_four")
    )
    card = (
        await session.scalars(select(Card).where(Card.owner_id == owner_id, Card.card_last_four == last4))
    ).one()
    if bank_name and not card.bank_name:
        await session.execute(update(Card).where(Card.id == card.id, Card.bank_name.is_(None)).values(bank_name=bank_name))
        card.bank_name = bank_name
    return card


async def record_balance_snapshot(
    session: AsyncSession,
    card: Card,
    *,
    balance: Decimal,
    currency: str,
    recorded_at: datetime,
    source: str,
    source_message_id: int | None = None,
    sync_request_id: int | None = None,
) -> bool:
    """Store a snapshot and move the card's current balance forward if this is the newest one.

    Idempotent: the same message never creates two snapshots. Out-of-order arrivals never overwrite
    a newer balance (the conditional UPDATE is atomic, so concurrent writers cannot interleave).
    Returns True if a new snapshot was stored.
    """
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    res = await session.execute(
        insert(BalanceSnapshot)
        .values(
            card_id=card.id,
            balance=balance,
            currency=currency,
            source=source,
            recorded_at=recorded_at,
            source_message_id=source_message_id,
            sync_request_id=sync_request_id,
        )
        .on_conflict_do_nothing(constraint="uq_balance_snapshots_msg")
        .returning(BalanceSnapshot.id)
    )
    inserted = res.scalar_one_or_none() is not None
    if currency == card.currency:
        await session.execute(
            update(Card)
            .where(
                Card.id == card.id,
                or_(Card.last_balance_sync_at.is_(None), Card.last_balance_sync_at <= recorded_at),
            )
            .values(current_balance=balance, last_balance_sync_at=recorded_at)
        )
    return inserted


async def list_cards(session: AsyncSession, owner_id: int | None = None) -> list[Card]:
    stmt = select(Card).where(Card.is_active.is_(True)).order_by(Card.id)
    if owner_id is not None:
        stmt = stmt.where(Card.owner_id == owner_id)
    return list((await session.scalars(stmt)).all())


def totals_by_currency(cards: list[Card]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for c in cards:
        if c.current_balance is not None:
            totals[c.currency] = totals.get(c.currency, Decimal("0")) + c.current_balance
    return totals
