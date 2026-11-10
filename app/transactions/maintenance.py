"""Idempotent data upkeep run at startup, so improved rules also apply to already-stored data.

Never touches owner-edited transactions and never deletes anything.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func, or_, select

from app.database.session import SessionFactory
from app.models import Category, TelegramMessage, Transaction
from app.models.enums import ClassificationStatus, MsgKind, MsgStatus, TxDirection, TxSource, TxType
from app.transactions.categories import resolve_classification
from app.transactions.parser import detect_kind
from app.transactions.rules import looks_like_own_account, looks_like_p2p
from app.transactions.service import HUMO_WRITE_LOCK, Classification, apply_classification, link_own_card_transfer

log = logging.getLogger(__name__)


async def upgrade_existing_data(sf: SessionFactory) -> None:
    async with sf.begin() as s:
        await s.execute(select(func.pg_advisory_xact_lock(HUMO_WRITE_LOCK)))

        # 1. Messages the parser can now understand (e.g. "Karta hisobi o'zgardi") get processed again.
        requeued = 0
        for m in (await s.scalars(select(TelegramMessage).where(TelegramMessage.kind == MsgKind.UNKNOWN))).all():
            kind = detect_kind(m.message_text)
            if kind != MsgKind.UNKNOWN:
                m.kind = kind
                m.processing_status = MsgStatus.RECEIVED
                m.processing_error = None
                m.next_attempt_at = None
                m.attempts = 0
                requeued += 1

        # 2. Money that arrived via P2P from other people is income, not a transfer.
        own = (
            await s.scalars(
                select(Category.id).where(Category.normalized_name == "own card", Category.parent_id.is_not(None))
            )
        ).first()
        incoming = (
            await s.scalars(
                select(Transaction).where(
                    Transaction.direction == TxDirection.IN,
                    Transaction.source == TxSource.HUMO,
                    Transaction.transaction_type == TxType.TRANSFER,
                    Transaction.user_edited.is_(False),
                    Transaction.deleted_at.is_(None),
                    or_(Transaction.subcategory_id.is_(None), Transaction.subcategory_id != (own or 0)),
                )
            )
        ).all()
        to_income = [t for t in incoming if looks_like_p2p(t.merchant)]
        if to_income:
            parent, child = await resolve_classification(s, "Income", "Transfers Received", TxType.INCOME)
            for t in to_income:
                await apply_classification(
                    s, t.id,
                    Classification(TxType.INCOME, parent, child, False, t.description, Decimal("1"), False,
                                   ClassificationStatus.RULE),
                )

        # 2b. Money sent to other people's cards is a transfer ("Sent to Others"), not an expense/"Other".
        outgoing = (
            await s.scalars(
                select(Transaction).where(
                    Transaction.direction == TxDirection.OUT,
                    Transaction.source == TxSource.HUMO,
                    Transaction.user_edited.is_(False),
                    Transaction.has_splits.is_(False),
                    Transaction.deleted_at.is_(None),
                    or_(Transaction.subcategory_id.is_(None), Transaction.subcategory_id != (own or 0)),
                )
            )
        ).all()
        sent = [t for t in outgoing if looks_like_p2p(t.merchant)]
        if sent:
            parent, child = await resolve_classification(s, "Transfers", "Sent to Others", TxType.TRANSFER)
            _, own_child = await resolve_classification(s, "Transfers", "Own Cards", TxType.TRANSFER)
            for t in sent:
                if looks_like_own_account(t.merchant):
                    if t.subcategory_id != own_child.id:
                        await apply_classification(s, t.id, Classification(
                            TxType.TRANSFER, parent, own_child, False, t.description, Decimal("1"), False,
                            ClassificationStatus.RULE))
                    continue
                if t.subcategory_id == child.id:
                    continue
                await apply_classification(
                    s, t.id,
                    Classification(TxType.TRANSFER, parent, child, False, t.description, Decimal("1"), False,
                                   ClassificationStatus.RULE),
                )

        # 3. Link transfers between my own cards (both sides become "Transfers / Own Cards").
        linked = 0
        candidates = (
            await s.scalars(
                select(Transaction)
                .where(
                    Transaction.source == TxSource.HUMO,
                    Transaction.card_id.is_not(None),
                    Transaction.direction == TxDirection.IN,
                    Transaction.deleted_at.is_(None),
                    Transaction.user_edited.is_(False),
                    or_(Transaction.subcategory_id.is_(None), Transaction.subcategory_id != (own or 0)),
                )
                .order_by(Transaction.transaction_datetime)
            )
        ).all()
        for t in candidates:
            if await link_own_card_transfer(s, t) is not None:
                linked += 1

    if requeued or to_income or linked:
        log.info("data upkeep: %s message(s) re-queued, %s incoming transfer(s) -> income, %s own-card transfer(s) linked",
                 requeued, len(to_income), linked)
