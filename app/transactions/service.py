"""Financial record writes. All amounts are Decimal; all writes are idempotent."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.cards.service import ensure_card, record_balance_snapshot
from app.models import AuditLog, Card, Category, TelegramMessage, Transaction
from app.models.enums import ClassificationStatus, SnapshotSource, TxDirection, TxSource, TxType
from app.transactions.categories import resolve_classification, resolve_full, unclassified
from app.transactions.parser import ParsedTransaction
from app.transactions.rules import ensure_merchant, looks_like_own_account, looks_like_p2p, set_owner_rule
from app.utils.timeutil import utcnow


HUMO_WRITE_LOCK = 7_202_612  # serializes HUMO record creation so duplicate/own-card checks are race-free
TWIN_WINDOW = timedelta(minutes=3)
OWN_TRANSFER_WINDOW = timedelta(minutes=5)
# The sending side includes a commission that varies (percentage, sometimes with a fixed minimum):
# accept up to 3% or 5 000 so'm, whichever is larger.
OWN_TRANSFER_FEE_RATE = Decimal("0.03")
OWN_TRANSFER_FEE_MIN = Decimal("5000")


def _max_fee(amount: Decimal) -> Decimal:
    return max(amount * OWN_TRANSFER_FEE_RATE, OWN_TRANSFER_FEE_MIN)


def fallback_type(parsed: ParsedTransaction) -> TxType:
    """Deterministic best effort used before/without AI."""
    if parsed.direction == TxDirection.IN:
        return TxType.INCOME  # money arriving (salary, top-ups, P2P from others) is income by default
    if looks_like_p2p(parsed.merchant):
        return TxType.TRANSFER
    return TxType.EXPENSE


def deterministic_rule(parsed: ParsedTransaction) -> tuple[str, str, str] | None:
    """Messages we can classify without AI: (transaction type, category, subcategory)."""
    if parsed.direction == TxDirection.IN:
        if parsed.generic:
            return (TxType.INCOME, "Income", "Card Top-up")
        if looks_like_p2p(parsed.merchant):
            return (TxType.INCOME, "Income", "Transfers Received")
        return None
    if looks_like_own_account(parsed.merchant):
        return (TxType.TRANSFER, "Transfers", "Own Cards")
    if looks_like_p2p(parsed.merchant):
        # Money sent to someone else's card. If the other side turns out to be one of my own cards,
        # link_own_card_transfer() re-labels both sides as "Own Cards".
        return (TxType.TRANSFER, "Transfers", "Sent to Others")
    return None


async def _find_twin(session: AsyncSession, card_id: int, amount: Decimal, when: datetime, *, generic: bool) -> Transaction | None:
    """Another notification of the same incoming event: same card and amount within a few minutes.
    generic=True looks for a short "Karta hisobi o'zgardi" record (no merchant), False for a detailed one."""
    stmt = select(Transaction).where(
        Transaction.card_id == card_id,
        Transaction.amount == amount,
        Transaction.direction == TxDirection.IN,
        Transaction.source == TxSource.HUMO,
        Transaction.deleted_at.is_(None),
        Transaction.transaction_datetime.between(when - TWIN_WINDOW, when + TWIN_WINDOW),
        Transaction.merchant.is_(None) if generic else Transaction.merchant.is_not(None),
    )
    return (await session.scalars(stmt.limit(1))).first()


async def create_humo_transaction(
    session: AsyncSession, message: TelegramMessage, parsed: ParsedTransaction, owner_id: int
) -> Transaction | None:
    """Create the financial record for a HUMO message exactly once.

    Returns None when the message is a duplicate notification of an event already recorded
    (HUMO sometimes sends both a detailed message and a short "Karta hisobi o'zgardi" notice).
    Uses only parser facts; the reported balance becomes a snapshot, never a transaction.
    """
    await session.execute(select(func.pg_advisory_xact_lock(HUMO_WRITE_LOCK)))
    existing = (await session.scalars(select(Transaction).where(Transaction.original_message_id == message.id))).first()
    if existing is not None:
        return existing

    card = None
    if parsed.card_last_four:
        card = await ensure_card(session, owner_id, parsed.card_last_four, currency=parsed.currency)
    tx_dt = parsed.transaction_datetime or message.message_date

    if parsed.generic and card is not None and await _find_twin(session, card.id, parsed.amount, tx_dt, generic=False):
        return None  # the detailed message already recorded this money

    merchant = await ensure_merchant(session, parsed.merchant, parsed.direction) if parsed.merchant else None
    tx_type = fallback_type(parsed)
    rule = deterministic_rule(parsed)
    if rule:
        tx_type = rule[0]
        category, subcategory = await resolve_classification(session, rule[1], rule[2], tx_type)
        status, review = ClassificationStatus.RULE, False
    else:
        category, subcategory = await unclassified(session)
        status, review = ClassificationStatus.PENDING, True

    res = await session.execute(
        insert(Transaction)
        .values(
            card_id=card.id if card else None,
            transaction_type=tx_type,
            direction=parsed.direction,
            amount=parsed.amount,
            currency=parsed.currency,
            merchant_id=merchant.id if merchant else None,
            merchant=parsed.merchant,
            operation_title=parsed.operation_title,
            category_id=category.id,
            subcategory_id=subcategory.id if subcategory else None,
            is_spending=tx_type == TxType.EXPENSE,
            transaction_datetime=tx_dt,
            source=TxSource.HUMO,
            classification_status=status,
            needs_review=review,
            reported_balance=parsed.reported_balance,
            original_message_id=message.id,
        )
        .on_conflict_do_nothing(index_elements=[Transaction.original_message_id])
        .returning(Transaction.id)
    )
    res.scalar_one_or_none()
    tx = (await session.scalars(select(Transaction).where(Transaction.original_message_id == message.id))).one()

    # A detailed message replaces an earlier short notice of the same money (kept in audit history).
    if not parsed.generic and parsed.direction == TxDirection.IN and card is not None:
        twin = await _find_twin(session, card.id, parsed.amount, tx_dt, generic=True)
        if twin is not None:
            twin.deleted_at = utcnow()
            await audit(session, "transaction", twin.id, "duplicate", {"replaced_by": (None, tx.id)}, "system")

    if card is not None:
        await link_own_card_transfer(session, tx)

    if card is not None and parsed.reported_balance is not None:
        await record_balance_snapshot(
            session,
            card,
            balance=parsed.reported_balance,
            currency=parsed.balance_currency or parsed.currency,
            recorded_at=message.message_date,
            source=SnapshotSource.TRANSACTION_MESSAGE,
            source_message_id=message.id,
        )
    await session.refresh(tx)
    return tx


async def link_own_card_transfer(session: AsyncSession, tx: Transaction) -> Transaction | None:
    """Money leaving one of my cards and arriving on another of my cards is a transfer to myself:
    it must count neither as spending nor as income."""
    if tx.card_id is None or tx.deleted_at is not None or tx.user_edited:
        return None
    if tx.direction == TxDirection.IN:  # the matching outgoing side = received amount + commission
        low, high = tx.amount, tx.amount + _max_fee(tx.amount)
        other_dir = TxDirection.OUT
    else:  # the matching incoming side = sent amount - commission
        low, high = max(Decimal("0.01"), tx.amount - _max_fee(tx.amount)), tx.amount
        other_dir = TxDirection.IN
    own_cards = select(Card.id).where(Card.owner_id == select(Card.owner_id).where(Card.id == tx.card_id).scalar_subquery())
    other = (
        await session.scalars(
            select(Transaction)
            .where(
                Transaction.card_id.in_(own_cards),
                Transaction.card_id != tx.card_id,
                Transaction.direction == other_dir,
                Transaction.amount.between(low, high),
                Transaction.source == TxSource.HUMO,
                Transaction.deleted_at.is_(None),
                Transaction.user_edited.is_(False),
                Transaction.transaction_datetime.between(
                    tx.transaction_datetime - OWN_TRANSFER_WINDOW, tx.transaction_datetime + OWN_TRANSFER_WINDOW
                ),
            )
            .order_by(func.abs(func.extract("epoch", Transaction.transaction_datetime - tx.transaction_datetime)))
            .limit(1)
        )
    ).first()
    if other is None:
        return None
    parent, child = await resolve_classification(session, "Transfers", "Own Cards", TxType.TRANSFER)
    for t in (tx, other):
        await apply_classification(
            session, t.id,
            Classification(
                transaction_type=TxType.TRANSFER, category=parent, subcategory=child, is_spending=False,
                description="Between my cards", confidence=Decimal("1"), needs_review=False,
                status=ClassificationStatus.RULE,
            ),
        )
    return other


@dataclass
class Classification:
    transaction_type: str
    category: Category
    subcategory: Category | None
    is_spending: bool
    description: str | None
    confidence: Decimal | None
    needs_review: bool
    status: ClassificationStatus


async def apply_classification(session: AsyncSession, tx_id: int, c: Classification) -> bool:
    """Write classification fields only. Amount/card/time/balance are never touched here, and
    owner edits are never overwritten."""
    res = await session.execute(
        update(Transaction)
        .where(Transaction.id == tx_id, Transaction.user_edited.is_(False))
        .values(
            transaction_type=c.transaction_type,
            category_id=c.category.id,
            subcategory_id=c.subcategory.id if c.subcategory else None,
            is_spending=c.is_spending,
            description=c.description,
            ai_confidence=c.confidence,
            needs_review=c.needs_review,
            classification_status=c.status,
            updated_at=utcnow(),
        )
    )
    return res.rowcount == 1


def _ser(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


async def audit(session: AsyncSession, entity_type: str, entity_id: int, action: str, changes: dict, actor: str) -> None:
    session.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            # changes: {field: (old, new)}
            changes={k: [_ser(old), _ser(new)] for k, (old, new) in changes.items()},
            actor=actor,
        )
    )


class TransactionNotFound(LookupError):
    pass


async def update_transaction_manual(
    session: AsyncSession,
    tx_id: int,
    *,
    actor: str,
    category: str | None = None,
    subcategory: str | None = None,
    transaction_type: str | None = None,
    description: str | None = None,
    apply_to_merchant: bool = False,
    category_id: int | None = None,
    subcategory_id: int | None = None,
) -> Transaction:
    """Owner correction of classification fields, with an audit trail. Locks the row."""
    tx = (
        await session.scalars(select(Transaction).where(Transaction.id == tx_id, Transaction.deleted_at.is_(None)).with_for_update())
    ).first()
    if tx is None:
        raise TransactionNotFound(tx_id)

    changes: dict = {}
    new_type = transaction_type or tx.transaction_type
    if new_type not in {t.value for t in TxType}:
        raise ValueError("invalid transaction type")
    if new_type != tx.transaction_type:
        changes["transaction_type"] = (tx.transaction_type, new_type)
        tx.transaction_type = new_type
        tx.is_spending = new_type == TxType.EXPENSE

    parent = child = None
    if category_id is not None:
        parent = await session.get(Category, category_id)
        if parent is None or parent.parent_id is not None:
            raise ValueError("invalid category")
        if subcategory_id is not None:
            child = await session.get(Category, subcategory_id)
            if child is None or child.parent_id != parent.id:
                raise ValueError("subcategory does not belong to category")
    elif category is not None:
        parent, child = await resolve_classification(session, category, subcategory, new_type)
    if parent is not None:
        if parent.id != tx.category_id:
            changes["category_id"] = (tx.category_id, parent.id)
        new_sub = child.id if child else None
        if new_sub != tx.subcategory_id:
            changes["subcategory_id"] = (tx.subcategory_id, new_sub)
        if parent.id != tx.category_id or new_sub != tx.subcategory_id:
            tx.item_id = None  # an item belongs to the old subcategory
        tx.category_id, tx.subcategory_id = parent.id, new_sub

    if description is not None:
        description = description.strip()[:500] or None
        if description != tx.description:
            changes["description"] = (tx.description, description)
            tx.description = description

    if changes or tx.needs_review:
        tx.user_edited = True
        tx.needs_review = False
        tx.classification_status = ClassificationStatus.MANUAL
        tx.updated_at = utcnow()
        await audit(session, "transaction", tx.id, "update", changes, actor)

    if apply_to_merchant and tx.merchant and tx.category_id:
        await set_owner_rule(
            session,
            tx.merchant,
            direction=tx.direction,
            category_id=tx.category_id,
            subcategory_id=tx.subcategory_id,
            transaction_type=tx.transaction_type,
            is_spending=tx.is_spending,
            now=utcnow(),
        )
    return tx


async def soft_delete_transaction(session: AsyncSession, tx_id: int, *, actor: str, reason: str | None) -> None:
    tx = (await session.scalars(select(Transaction).where(Transaction.id == tx_id).with_for_update())).first()
    if tx is None or tx.deleted_at is not None:
        raise TransactionNotFound(tx_id)
    if tx.source == TxSource.HUMO:
        raise ValueError("Bank transactions cannot be deleted; change their type instead")
    tx.deleted_at = utcnow()
    await audit(session, "transaction", tx.id, "delete", {"reason": (None, reason)}, actor)


async def create_manual_transaction(
    session: AsyncSession,
    *,
    transaction_type: TxType,
    amount: Decimal,
    currency: str,
    occurred_at: datetime,
    description: str | None,
    category: str | None,
    subcategory: str | None,
    source: TxSource,
    idempotency_key: str,
    card_id: int | None = None,
    item: str | None = None,
) -> tuple[Transaction, bool]:
    """Expense/income reported by the owner (text/voice). Does not change any card balance."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    parent, child, item_cat = await resolve_full(session, category, subcategory, item, transaction_type)
    direction = TxDirection.IN if transaction_type in (TxType.INCOME, TxType.REFUND) else TxDirection.OUT
    res = await session.execute(
        insert(Transaction)
        .values(
            card_id=card_id,
            transaction_type=transaction_type,
            direction=direction,
            amount=amount,
            currency=currency,
            description=(description or None) and description[:500],
            category_id=parent.id,
            subcategory_id=child.id if child else None,
            item_id=item_cat.id if item_cat else None,
            is_spending=transaction_type == TxType.EXPENSE,
            transaction_datetime=occurred_at,
            source=source,
            classification_status=ClassificationStatus.MANUAL,
            needs_review=False,
            user_edited=True,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=[Transaction.idempotency_key])
        .returning(Transaction.id)
    )
    created = res.scalar_one_or_none() is not None
    tx = (await session.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key))).one()
    return tx, created


async def purpose_text(session: AsyncSession, debt) -> str:
    names = []
    for cid in (debt.category_id, debt.subcategory_id, debt.item_id):
        if cid:
            c = await session.get(Category, cid)
            if c:
                names.append(c.name)
    return " › ".join(names) or (debt.description or "debt")


async def create_expense_for_debt(session: AsyncSession, debt, amount: Decimal, when: datetime, source: str,
                                  idempotency_key: str, person_name: str) -> Transaction:
    """Spending recognised when repaying a debt that was for a purchase (e.g. Coca-Cola bought by a friend).
    Not linked to any card; card balances are untouched."""
    res = await session.execute(
        insert(Transaction)
        .values(
            transaction_type=TxType.EXPENSE, direction=TxDirection.OUT, amount=amount, currency=debt.currency,
            description=f"{person_name} — debt repaid"[:500], category_id=debt.category_id,
            subcategory_id=debt.subcategory_id, item_id=debt.item_id, is_spending=True,
            transaction_datetime=when, source=source, classification_status=ClassificationStatus.MANUAL,
            needs_review=False, user_edited=True, idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=[Transaction.idempotency_key])
        .returning(Transaction.id)
    )
    res.scalar_one_or_none()
    return (await session.scalars(select(Transaction).where(Transaction.idempotency_key == idempotency_key))).one()
