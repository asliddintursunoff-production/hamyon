"""Personal debts. Debts are never income or expenses; they live in their own ledger."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Debt, DebtPayment, Person
from app.models.enums import DebtDirection, DebtStatus
from app.transactions.service import audit
from app.utils.text import normalize_name, similarity, strip_uz_suffix
from app.utils.timeutil import utcnow

ZERO = Decimal("0.00")


class DebtError(ValueError):
    pass


class OverpaymentError(DebtError):
    def __init__(self, outstanding: Decimal):
        super().__init__(f"payment exceeds outstanding amount {outstanding}")
        self.outstanding = outstanding


class NothingToRepay(DebtError):
    pass


def clean_person_name(name: str) -> str:
    name = " ".join((name or "").split()).strip(" .,;:!?")
    if not name or len(name) > 100 or not any(ch.isalpha() for ch in name):
        raise DebtError("invalid person name")
    return name[0].upper() + name[1:]


async def match_person(session: AsyncSession, name: str) -> Person | None:
    """Find an existing person: exact, Uzbek-suffix-insensitive, or a very close spelling."""
    key = normalize_name(name)
    people = list((await session.scalars(select(Person))).all())
    for p in people:
        if p.normalized_name == key:
            return p
    stem = strip_uz_suffix(key)
    for p in people:
        if strip_uz_suffix(p.normalized_name) == stem:
            return p
    if len(key) >= 5:
        best = max(people, key=lambda p: similarity(key, p.normalized_name), default=None)
        if best is not None and similarity(key, best.normalized_name) >= 0.9:
            return best
    return None


async def find_or_create_person(session: AsyncSession, name: str) -> tuple[Person, bool]:
    display = clean_person_name(name)
    existing = await match_person(session, display)
    if existing:
        return existing, False
    key = normalize_name(display)
    res = await session.execute(
        insert(Person).values(name=display, normalized_name=key).on_conflict_do_nothing().returning(Person.id)
    )
    created = res.scalar_one_or_none() is not None
    person = (await session.scalars(select(Person).where(Person.normalized_name == key))).one()
    return person, created


async def create_debt(
    session: AsyncSession,
    *,
    person_name: str,
    direction: DebtDirection,
    amount: Decimal,
    currency: str,
    description: str | None,
    occurred_at: datetime,
    source: str,
    idempotency_key: str | None,
    category: str | None = None,
    subcategory: str | None = None,
    item: str | None = None,
) -> tuple[Debt, bool]:
    """Record a debt. `category/subcategory/item` say what it was for (e.g. Food › Snacks › Coca-Cola);
    that is NOT spending yet — it becomes spending when the debt is repaid."""
    if direction not in (DebtDirection.I_OWE_THEM, DebtDirection.THEY_OWE_ME):
        raise DebtError("debt direction is required")
    if amount is None or amount <= 0:
        raise DebtError("amount must be positive")
    if occurred_at.tzinfo is None:
        raise DebtError("occurred_at must be timezone-aware")
    person, _ = await find_or_create_person(session, person_name)
    cat_id = sub_id = item_id = None
    if category:
        from app.transactions.categories import resolve_full

        cat, sub, it = await resolve_full(session, category, subcategory, item, "expense")
        cat_id, sub_id, item_id = cat.id, sub.id if sub else None, it.id if it else None
    res = await session.execute(
        insert(Debt)
        .values(
            person_id=person.id,
            direction=direction,
            original_amount=amount,
            currency=currency,
            category_id=cat_id,
            subcategory_id=sub_id,
            item_id=item_id,
            description=(description or None) and description[:500],
            occurred_at=occurred_at,
            status=DebtStatus.OPEN,
            source=source,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=[Debt.idempotency_key])
        .returning(Debt.id)
    )
    new_id = res.scalar_one_or_none()
    if new_id is None:
        debt = (await session.scalars(select(Debt).where(Debt.idempotency_key == idempotency_key))).one()
        return debt, False
    return await session.get(Debt, new_id), True


async def _paid_by_debt(session: AsyncSession, debt_ids: list[int]) -> dict[int, Decimal]:
    if not debt_ids:
        return {}
    rows = await session.execute(
        select(DebtPayment.debt_id, func.coalesce(func.sum(DebtPayment.amount), 0))
        .where(DebtPayment.debt_id.in_(debt_ids), DebtPayment.cancelled_at.is_(None))
        .group_by(DebtPayment.debt_id)
    )
    return {debt_id: Decimal(total) for debt_id, total in rows.all()}


async def outstanding_of(session: AsyncSession, debt: Debt) -> Decimal:
    paid = (await _paid_by_debt(session, [debt.id])).get(debt.id, ZERO)
    return debt.original_amount - paid


@dataclass
class RepaymentResult:
    payments: list[DebtPayment]
    allocations: list[tuple[Debt, Decimal]]  # which debt each part of the money paid off (oldest first)
    excess_debt: Debt | None = None  # overpayment recorded as a debt in the other direction

    @property
    def excess(self) -> Decimal:
        return self.excess_debt.original_amount if self.excess_debt else ZERO


async def open_debts(session: AsyncSession, person_id: int, direction: DebtDirection, currency: str, *, lock: bool = False) -> list[Debt]:
    stmt = (
        select(Debt)
        .where(Debt.person_id == person_id, Debt.direction == direction, Debt.currency == currency,
               Debt.status == DebtStatus.OPEN)
        .order_by(Debt.occurred_at, Debt.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    return list((await session.scalars(stmt)).all())


async def outstanding_total(session: AsyncSession, person_id: int, direction: DebtDirection, currency: str) -> Decimal:
    debts = await open_debts(session, person_id, direction, currency)
    paid = await _paid_by_debt(session, [d.id for d in debts])
    return sum((d.original_amount - paid.get(d.id, ZERO) for d in debts), ZERO)


async def repay(
    session: AsyncSession,
    *,
    person_id: int,
    direction: DebtDirection,
    amount: Decimal,
    currency: str,
    paid_at: datetime,
    description: str | None,
    idempotency_key: str,
    allow_excess: bool = False,
    source: str = "bot_text",
) -> RepaymentResult:
    """Apply a (partial or full) repayment to the person's open debts, OLDEST FIRST.

    Open debts are locked FOR UPDATE, so concurrent repayments are serialized and can never pay more than
    is outstanding. With allow_excess, money beyond what was owed becomes a debt in the other direction
    ("I paid Orzubek 6 000 but owed 3 000 -> Orzubek now owes me 3 000"). Same key => same result.
    """
    if amount <= 0:
        raise DebtError("amount must be positive")
    debts = await open_debts(session, person_id, direction, currency, lock=True)
    existing = list((await session.scalars(select(DebtPayment).where(DebtPayment.idempotency_key == idempotency_key))).all())
    excess_key = f"{idempotency_key}:excess"
    if existing:
        by_id = {d.id: d for d in (await session.scalars(select(Debt).where(Debt.id.in_([p.debt_id for p in existing])))).all()}
        excess = (await session.scalars(select(Debt).where(Debt.idempotency_key == excess_key))).first()
        return RepaymentResult(existing, [(by_id[p.debt_id], p.amount) for p in existing], excess)

    paid = await _paid_by_debt(session, [d.id for d in debts])
    outstanding = {d.id: d.original_amount - paid.get(d.id, ZERO) for d in debts}
    total = sum(outstanding.values(), ZERO)
    if not debts and not allow_excess:
        raise NothingToRepay("no open debts in this direction")
    if amount > total and not allow_excess:
        raise OverpaymentError(total)

    remaining = amount
    payments: list[DebtPayment] = []
    allocations: list[tuple[Debt, Decimal]] = []
    for d in debts:
        if remaining <= 0:
            break
        part = min(remaining, outstanding[d.id])
        if part <= 0:
            continue
        p = DebtPayment(
            debt_id=d.id, amount=part, payment_date=paid_at,
            description=(description or None) and description[:500], idempotency_key=idempotency_key,
        )
        session.add(p)
        payments.append(p)
        allocations.append((d, part))
        remaining -= part
        if part == outstanding[d.id]:
            d.status = DebtStatus.SETTLED
            d.updated_at = utcnow()
    excess_debt = None
    if remaining > 0:
        person = await session.get(Person, person_id)
        other = DebtDirection.THEY_OWE_ME if direction == DebtDirection.I_OWE_THEM else DebtDirection.I_OWE_THEM
        excess_debt, _ = await create_debt(
            session, person_name=person.name, direction=other, amount=remaining, currency=currency,
            description="overpaid when repaying", occurred_at=paid_at, source=source, idempotency_key=excess_key,
        )
    await session.flush()
    return RepaymentResult(payments, allocations, excess_debt)


async def record_repayment(
    session: AsyncSession,
    *,
    person_id: int,
    direction: DebtDirection,
    amount: Decimal,
    currency: str,
    paid_at: datetime,
    description: str | None,
    idempotency_key: str,
) -> list[DebtPayment]:
    """Strict repayment: refuses to pay more than is owed."""
    result = await repay(
        session, person_id=person_id, direction=direction, amount=amount, currency=currency, paid_at=paid_at,
        description=description, idempotency_key=idempotency_key,
    )
    return result.payments


def purpose_of(debt: Debt) -> str | None:
    """'Food › Snacks › Coca-Cola' for a debt with a recorded purpose (relationships must be loaded)."""
    names = [c.name for c in (debt.category, debt.subcategory, debt.item) if c is not None]
    return " › ".join(names) or None


async def cancel_debt(session: AsyncSession, debt_id: int, *, reason: str | None, actor: str) -> Debt:
    debt = (await session.scalars(select(Debt).where(Debt.id == debt_id).with_for_update())).first()
    if debt is None:
        raise DebtError("debt not found")
    if debt.status == DebtStatus.CANCELLED:
        return debt
    await audit(session, "debt", debt.id, "cancel", {"status": (debt.status, DebtStatus.CANCELLED), "reason": (None, reason)}, actor)
    debt.status = DebtStatus.CANCELLED
    debt.cancelled_at = utcnow()
    debt.cancel_reason = reason
    return debt


async def cancel_payment(session: AsyncSession, payment_id: int, *, reason: str | None, actor: str) -> DebtPayment:
    payment = await session.get(DebtPayment, payment_id)
    if payment is None:
        raise DebtError("payment not found")
    debt = (await session.scalars(select(Debt).where(Debt.id == payment.debt_id).with_for_update())).one()
    await session.refresh(payment)
    if payment.cancelled_at is not None:
        return payment
    payment.cancelled_at = utcnow()
    await audit(session, "debt_payment", payment.id, "cancel", {"amount": (str(payment.amount), None), "reason": (None, reason)}, actor)
    if debt.status == DebtStatus.SETTLED:
        debt.status = DebtStatus.OPEN
    return payment


async def correct_debt(
    session: AsyncSession, debt_id: int, *, amount: Decimal | None = None, description: str | None = None, actor: str
) -> Debt:
    debt = (await session.scalars(select(Debt).where(Debt.id == debt_id).with_for_update())).first()
    if debt is None or debt.status == DebtStatus.CANCELLED:
        raise DebtError("debt not found")
    changes = {}
    if amount is not None and amount != debt.original_amount:
        if amount <= 0:
            raise DebtError("amount must be positive")
        paid = (await _paid_by_debt(session, [debt.id])).get(debt.id, ZERO)
        if amount < paid:
            raise DebtError(f"amount cannot be less than already repaid {paid}")
        changes["original_amount"] = (debt.original_amount, amount)
        debt.original_amount = amount
        debt.status = DebtStatus.SETTLED if amount == paid else DebtStatus.OPEN
    if description is not None and description != debt.description:
        changes["description"] = (debt.description, description)
        debt.description = description[:500]
    if changes:
        debt.updated_at = utcnow()
        await audit(session, "debt", debt.id, "update", changes, actor)
    return debt


@dataclass
class PersonBalance:
    person_id: int
    name: str
    currency: str
    they_owe_me: Decimal = ZERO
    i_owe_them: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        """Positive: they owe me overall. Negative: I owe them overall."""
        return self.they_owe_me - self.i_owe_them


@dataclass
class DebtOverview:
    people: list[PersonBalance] = field(default_factory=list)

    def totals(self, currency: str) -> tuple[Decimal, Decimal, Decimal]:
        owed_to_me = sum((p.they_owe_me for p in self.people if p.currency == currency), ZERO)
        i_owe = sum((p.i_owe_them for p in self.people if p.currency == currency), ZERO)
        return owed_to_me, i_owe, owed_to_me - i_owe


async def debt_overview(session: AsyncSession, include_settled: bool = False) -> DebtOverview:
    paid_sq = (
        select(DebtPayment.debt_id, func.sum(DebtPayment.amount).label("paid"))
        .where(DebtPayment.cancelled_at.is_(None))
        .group_by(DebtPayment.debt_id)
        .subquery()
    )
    outstanding = Debt.original_amount - func.coalesce(paid_sq.c.paid, 0)
    rows = await session.execute(
        select(Person.id, Person.name, Debt.currency, Debt.direction, func.sum(outstanding))
        .join(Debt, Debt.person_id == Person.id)
        .outerjoin(paid_sq, paid_sq.c.debt_id == Debt.id)
        .where(Debt.status != DebtStatus.CANCELLED)
        .group_by(Person.id, Person.name, Debt.currency, Debt.direction)
    )
    acc: dict[tuple[int, str], PersonBalance] = {}
    for pid, name, currency, direction, total in rows.all():
        pb = acc.setdefault((pid, currency), PersonBalance(pid, name, currency))
        if direction == DebtDirection.THEY_OWE_ME:
            pb.they_owe_me += Decimal(total)
        else:
            pb.i_owe_them += Decimal(total)
    people = [p for p in acc.values() if include_settled or p.they_owe_me or p.i_owe_them]
    people.sort(key=lambda p: (-abs(p.net), p.name))
    return DebtOverview(people)


async def person_history(session: AsyncSession, person_id: int) -> dict:
    person = await session.get(Person, person_id)
    if person is None:
        raise DebtError("person not found")
    from sqlalchemy.orm import selectinload

    debts = list((await session.scalars(
        select(Debt)
        .options(selectinload(Debt.category), selectinload(Debt.subcategory), selectinload(Debt.item))
        .where(Debt.person_id == person_id).order_by(Debt.occurred_at, Debt.id)
    )).all())
    payments = list(
        (await session.scalars(select(DebtPayment).where(DebtPayment.debt_id.in_([d.id for d in debts] or [0])).order_by(DebtPayment.payment_date))).all()
    )
    paid = defaultdict(lambda: ZERO)
    for p in payments:
        if p.cancelled_at is None:
            paid[p.debt_id] += p.amount
    events = []
    for d in debts:
        events.append({
            "type": "debt", "id": d.id, "date": d.occurred_at, "direction": d.direction, "amount": d.original_amount,
            "currency": d.currency, "description": d.description, "status": d.status,
            "purpose": purpose_of(d),
            "outstanding": ZERO if d.status == DebtStatus.CANCELLED else d.original_amount - paid[d.id],
        })
    by_id = {d.id: d for d in debts}
    for p in payments:
        events.append({
            "type": "payment", "id": p.id, "debt_id": p.debt_id, "date": p.payment_date,
            "direction": by_id[p.debt_id].direction, "amount": p.amount, "currency": by_id[p.debt_id].currency,
            "description": p.description, "status": "cancelled" if p.cancelled_at else "ok",
            "purpose": purpose_of(by_id[p.debt_id]),
        })
    events.sort(key=lambda e: (e["date"], 0 if e["type"] == "debt" else 1))
    return {"person": person, "events": events}
