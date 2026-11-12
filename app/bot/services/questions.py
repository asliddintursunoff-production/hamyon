"""Rendering of "what was this money for?" messages: open question vs. answered (✅)."""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.keyboards.main import inline
from app.database.session import SessionFactory
from app.models import Transaction, TransactionQuestion, TransactionSplit
from app.models.enums import TxDirection
from app.utils.money import format_money
from app.utils.text import escape_html

log = logging.getLogger(__name__)

CHOICES_OUT = [("🍔 Food", "food"), ("🛒 Groceries", "groceries"), ("🚕 Taxi", "taxi"), ("🛍 Shopping", "shopping")]
CHOICES_IN = [("💰 Income", "income")]


def question_markup(tx_id: int, incoming: bool):
    buttons = [(label, f"q:{tx_id}:{key}") for label, key in (CHOICES_IN if incoming else CHOICES_OUT)]
    return inline([buttons, [("✖️", f"q:{tx_id}:keep")]])


def edit_markup(tx_id: int):
    return inline([[("✏️", f"q:{tx_id}:edit")]])


async def load_tx(sf: SessionFactory, tx_id: int) -> Transaction | None:
    async with sf() as s:
        return (
            await s.scalars(
                select(Transaction)
                .options(
                    selectinload(Transaction.category), selectinload(Transaction.subcategory), selectinload(Transaction.item),
                    selectinload(Transaction.splits).selectinload(TransactionSplit.category),
                    selectinload(Transaction.splits).selectinload(TransactionSplit.subcategory),
                    selectinload(Transaction.splits).selectinload(TransactionSplit.item),
                    selectinload(Transaction.splits).selectinload(TransactionSplit.person),
                )
                .where(Transaction.id == tx_id)
            )
        ).first()


def head(tx: Transaction) -> str:
    sign = "−" if tx.direction == TxDirection.OUT else "+"
    return f"{sign}{format_money(tx.amount, tx.currency)} · {escape_html(tx.merchant or tx.operation_title or 'payment')}"


def question_text(tx: Transaction) -> str:
    text = f"❓ <b>{head(tx)}</b>"
    cat = tx.category.name if tx.category else None
    if cat and cat != "Other" and tx.direction == TxDirection.OUT and tx.transaction_type == "expense":
        guess = " › ".join(c.name for c in (tx.category, tx.subcategory, tx.item) if c)
        text += f"\n<i>{escape_html(guess)}?</i>  ✖️ keeps it"
    return text


def _label(sp: TransactionSplit, direction: str) -> str:
    who = escape_html(sp.person.name) if sp.person else "?"
    if sp.kind == "debt_repay":
        base = f"🤝 {who} — debt repaid" if direction == TxDirection.OUT else f"🤝 {who} repaid you"
        if sp.transaction_type == "expense":  # repaid for something bought for me: counted as spending
            base += " · " + escape_html(" › ".join(c.name for c in (sp.category, sp.subcategory, sp.item) if c))
        return base
    if sp.kind == "debt_new":
        return f"🤝 Lent to {who}" if direction == TxDirection.OUT else f"🤝 Borrowed from {who}"
    return escape_html(" › ".join(c.name for c in (sp.category, sp.subcategory, sp.item) if c) or "Other")


def done_text(tx: Transaction) -> str:
    parts = tx.splits
    if len(parts) == 1:
        return f"✅ <b>{head(tx)}</b>\n{_label(parts[0], tx.direction)}"
    lines = [f"• {_label(p, tx.direction)} — {'≈' if p.estimated else ''}{format_money(p.amount, tx.currency)}" for p in parts]
    return f"✅ <b>{head(tx)}</b>\n" + "\n".join(lines)


async def sync_question_messages(bot: Bot, sf: SessionFactory, tx_id: int, *, editing: bool = False) -> None:
    """Show every question message about this payment as answered (✅ + ✏️) or open (❓ + choices)."""
    tx = await load_tx(sf, tx_id)
    if tx is None:
        return
    async with sf() as s:
        questions = (await s.scalars(select(TransactionQuestion).where(TransactionQuestion.transaction_id == tx_id))).all()
    incoming = tx.direction == TxDirection.IN
    if tx.has_splits:
        text = done_text(tx)
        markup = question_markup(tx.id, incoming) if editing else edit_markup(tx.id)
    else:
        text, markup = question_text(tx), question_markup(tx.id, incoming)
    for q in questions:
        try:
            await bot.edit_message_text(text, chat_id=q.chat_id, message_id=q.message_id, reply_markup=markup)
        except Exception as exc:  # "message is not modified" and similar are harmless
            if "not modified" not in str(exc):
                log.warning("could not update question message %s: %s", q.message_id, type(exc).__name__)
