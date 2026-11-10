"""Which transactions the bot should ask about ("what was this money for?")."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from app.database.session import SessionFactory
from app.models import Transaction, TransactionQuestion
from app.models.enums import TxType
from app.utils.timeutil import utcnow


def is_unclear(tx: Transaction) -> bool:
    """Money whose purpose the bank message doesn't reveal: sent to someone's card, or left in "Other"."""
    if tx.deleted_at is not None or tx.has_splits or tx.user_edited or tx.card_id is None:
        return False
    cat = tx.category.name if tx.category else None
    sub = tx.subcategory.name if tx.subcategory else None
    if tx.transaction_type == TxType.TRANSFER:
        return sub == "Sent to Others"
    return cat in (None, "Other")


async def load_for_question(sf: SessionFactory, tx_id: int) -> Transaction | None:
    async with sf() as s:
        tx = (
            await s.scalars(
                select(Transaction)
                .options(selectinload(Transaction.category), selectinload(Transaction.subcategory), selectinload(Transaction.card))
                .where(Transaction.id == tx_id)
            )
        ).first()
        if tx is None or not is_unclear(tx):
            return None
        asked = await s.scalar(select(TransactionQuestion.id).where(TransactionQuestion.transaction_id == tx_id).limit(1))
        return None if asked else tx


async def remember_question(sf: SessionFactory, tx_id: int, chat_id: int, message_id: int) -> None:
    async with sf.begin() as s:
        await s.execute(
            insert(TransactionQuestion)
            .values(transaction_id=tx_id, chat_id=chat_id, message_id=message_id)
            .on_conflict_do_nothing()
        )


async def find_question(sf: SessionFactory, chat_id: int, message_id: int) -> TransactionQuestion | None:
    async with sf() as s:
        return (
            await s.scalars(
                select(TransactionQuestion).where(
                    TransactionQuestion.chat_id == chat_id, TransactionQuestion.message_id == message_id
                )
            )
        ).first()


async def mark_answered(sf: SessionFactory, tx_id: int) -> None:
    from sqlalchemy import update

    async with sf.begin() as s:
        await s.execute(
            update(TransactionQuestion).where(TransactionQuestion.transaction_id == tx_id).values(answered_at=utcnow())
        )
