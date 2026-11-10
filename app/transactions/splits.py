"""Owner explanations of a bank transaction: one payment split into parts (categories and debts).

Examples for a −49 000 transfer:
  "ovqat"                                    -> 1 part: expense Food 49 000
  "20 ming ovqat, qolgani taksi"             -> expense Food 20 000 + expense Transport/Taxi 29 000
  "Orzubekka 20k qarzimni qaytardim, 29k ovqat" -> debt repayment 20 000 (debt ledger updated) + Food 29 000

All money facts come from the bank transaction; parts must add up to it exactly. Applying a new
explanation reverts the previous one (including its debt effects), with an audit trail.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import delete, select

from app.debts.service import (
    DebtError,
    repay,
    cancel_debt,
    cancel_payment,
    create_debt,
    match_person,
    outstanding_of,
    record_repayment,
)
from app.models import AuditLog, Debt, DebtPayment, Transaction, TransactionSplit
from app.models.enums import ClassificationStatus, DebtDirection, DebtStatus, TxDirection, TxType
from app.transactions.categories import resolve_classification, resolve_full
from app.transactions.service import audit
from app.utils.money import quantize
from app.utils.timeutil import utcnow

KIND_TYPE = {
    "expense": TxType.EXPENSE,
    "income": TxType.INCOME,
    "refund": TxType.REFUND,
    "transfer": TxType.TRANSFER,
    "debt_repay": TxType.TRANSFER,  # debts are never income/expense
    "debt_new": TxType.TRANSFER,
}
ALLOWED = {
    TxDirection.OUT: {"expense", "transfer", "debt_repay", "debt_new"},
    TxDirection.IN: {"income", "refund", "transfer", "debt_repay", "debt_new"},
}
KIND_LABEL = {
    "expense": "", "income": "Income", "refund": "Refund", "transfer": "Transfer",
    "debt_repay": "Debt repayment", "debt_new": "Loan",
}


class SplitError(ValueError):
    pass


def debt_direction(kind: str, tx_direction: str) -> DebtDirection:
    """Which debt ledger direction a debt part affects, given which way the money moved."""
    if kind == "debt_repay":  # out: I repaid my debt; in: they repaid theirs
        return DebtDirection.I_OWE_THEM if tx_direction == TxDirection.OUT else DebtDirection.THEY_OWE_ME
    # debt_new — out: I lent them money; in: I borrowed from them
    return DebtDirection.THEY_OWE_ME if tx_direction == TxDirection.OUT else DebtDirection.I_OWE_THEM


@dataclass
class PlannedLine:
    kind: str
    amount: Decimal
    category: str | None = None
    subcategory: str | None = None
    person_name: str | None = None
    description: str | None = None
    stated: bool = True  # False when the amount was inferred ("the rest" / whole transaction)
    item: str | None = None  # the specific thing: "Hot dog", "Kurtka"
    estimated: bool = False  # price not said by the owner; scaled so all parts add up to the payment

    def to_json(self) -> dict:
        d = asdict(self)
        d["amount"] = str(self.amount)
        return d

    @classmethod
    def from_json(cls, d: dict) -> PlannedLine:
        return cls(**{**d, "amount": Decimal(d["amount"])})


def _fit_estimates(planned: list[PlannedLine], total: Decimal, *, has_rest: bool) -> None:
    """Scale estimated prices so that, together with the prices the owner said, they use up the payment
    exactly (whole so'm). Owner-stated prices are never changed. With a "the rest" part present, estimates
    are only scaled down if they don't fit."""
    est = [p for p in planned if p.estimated]
    if not est:
        return
    fixed = sum((p.amount for p in planned if p.stated), Decimal("0"))
    room = total - fixed
    est_sum = sum((p.amount for p in est), Decimal("0"))
    if room <= 0:
        raise SplitError("the prices you said already use up the whole payment")
    if has_rest and est_sum < room:
        return  # estimates fit; "the rest" part takes what's left
    target = room - (Decimal("1") * (1 if has_rest else 0))  # leave at least 1 so'm for "the rest"
    used = Decimal("0")
    for p in est[:-1]:
        p.amount = max(Decimal("1"), (p.amount * target / est_sum).quantize(Decimal("1")))
        used += p.amount
    est[-1].amount = target - used
    if est[-1].amount <= 0:
        raise SplitError("could not estimate the prices, please say them")


def plan_lines(lines, total: Decimal, direction: str) -> tuple[list[PlannedLine], list[str]]:
    """Turn AI lines into exact parts. Returns (parts, problems). Raises SplitError if impossible."""
    allowed = ALLOWED[TxDirection(direction)]
    planned: list[PlannedLine] = []
    missing = [i for i, ln in enumerate(lines) if ln.amount is None]
    if len(missing) > 1:
        raise SplitError("more than one part has no amount — please say how much each part was")
    for ln in lines:
        if ln.kind not in allowed:
            what = "money that left your card" if direction == TxDirection.OUT else "money that arrived"
            raise SplitError(f"“{ln.kind}” does not fit {what}")
        if ln.kind in ("debt_repay", "debt_new") and not ln.person_name:
            raise SplitError("whose debt is it? please name the person")
        if ln.amount is not None and ln.amount <= 0:
            raise SplitError("amounts must be positive")
        estimated = bool(getattr(ln, "estimated", False)) and ln.amount is not None
        planned.append(PlannedLine(ln.kind, quantize(ln.amount) if ln.amount is not None else Decimal("0"),
                                   ln.category, ln.subcategory, ln.person_name, ln.description,
                                   stated=ln.amount is not None and not estimated, item=getattr(ln, "item", None),
                                   estimated=estimated))
    _fit_estimates(planned, total, has_rest=bool(missing))
    stated = sum((p.amount for p in planned if p.stated or p.estimated), Decimal("0"))
    problems: list[str] = []
    if stated > total:
        raise SplitError(f"the parts add up to {stated}, more than the payment of {total}")
    if missing:
        rest = total - stated
        if rest <= 0:
            raise SplitError("nothing is left for “the rest”")
        planned[missing[0]].amount = rest
    elif stated < total:
        if len(planned) == 1:
            problems.append(f"you said {stated}, but the payment was {total}; the whole payment will be used")
            planned[0].amount, planned[0].stated = total, False
        else:
            rest = total - stated
            problems.append(f"the parts add up to {stated}; the remaining {rest} will be kept as “Other”")
            planned.append(PlannedLine("transfer" if direction == TxDirection.OUT else "income", rest,
                                       "Other", None, None, "not explained", stated=False))
    return planned, problems


def is_paid_purchase(line: PlannedLine, tx_direction: str) -> bool:
    """Repaying someone for something they bought for me (water, food, taxi): that is my spending."""
    return (line.kind == "debt_repay" and tx_direction == TxDirection.OUT
            and bool(line.category) and line.category.strip().lower() not in ("debts", "debt", "transfers"))


def line_type(line: PlannedLine, tx_direction: str) -> str:
    return TxType.EXPENSE if is_paid_purchase(line, tx_direction) else KIND_TYPE[line.kind]


async def _category_for(session, line: PlannedLine, tx_type: str):
    if line.kind == "debt_repay" and tx_type == TxType.EXPENSE:
        return await resolve_full(session, line.category, line.subcategory, line.item, TxType.EXPENSE)
    if line.kind == "debt_repay":
        return (*await resolve_classification(session, "Debts", "Repayment", TxType.TRANSFER), None)
    if line.kind == "debt_new":
        return (*await resolve_classification(session, "Debts", "Loan", TxType.TRANSFER), None)
    if line.kind == "transfer" and not line.category:
        return (*await resolve_classification(session, "Transfers", "Sent to Others", TxType.TRANSFER), None)
    default = {"income": "Income", "refund": "Refunds"}.get(line.kind, "Other")
    return await resolve_full(session, line.category or default, line.subcategory, line.item, tx_type)


async def precheck_debts(session, tx: Transaction, lines: list[PlannedLine]) -> tuple[list[str], list[str]]:
    """(errors, notes) for debt parts: unknown person is an error; paying more than owed is a note to
    confirm (the extra is recorded as the other person owing)."""
    from app.debts.service import outstanding_total

    errors, notes = [], []
    for ln in lines:
        if ln.kind != "debt_repay":
            continue
        person = await match_person(session, ln.person_name)
        if person is None:
            errors.append(f"I don't have any debt with {ln.person_name}")
            continue
        direction = debt_direction(ln.kind, tx.direction)
        owed = await outstanding_total(session, person.id, direction, tx.currency)
        if ln.amount > owed:
            extra = ln.amount - owed
            if direction == DebtDirection.I_OWE_THEM:
                notes.append(f"you owed {person.name} {owed}; the extra {extra} will be recorded as {person.name} owing you")
            else:
                notes.append(f"{person.name} owed you {owed}; the extra {extra} will be recorded as you owing {person.name}")
    return errors, notes


async def _revert(session, tx: Transaction, actor: str) -> list[dict]:
    old = list((await session.scalars(
        select(TransactionSplit).where(TransactionSplit.transaction_id == tx.id).order_by(TransactionSplit.position)
    )).all())
    for sp in old:
        if sp.payment_key:
            for p in (await session.scalars(select(DebtPayment).where(DebtPayment.idempotency_key == sp.payment_key))).all():
                await cancel_payment(session, p.id, reason="explanation replaced", actor=actor)
        if sp.debt_id:
            await cancel_debt(session, sp.debt_id, reason="explanation replaced", actor=actor)
    await session.execute(delete(TransactionSplit).where(TransactionSplit.transaction_id == tx.id))
    return [{"kind": s.kind, "amount": str(s.amount), "category_id": s.category_id, "subcategory_id": s.subcategory_id,
             "person_id": s.person_id} for s in old]


def _snapshot(tx: Transaction) -> dict:
    return {
        "transaction_type": tx.transaction_type, "category_id": tx.category_id, "subcategory_id": tx.subcategory_id,
        "item_id": tx.item_id,
        "is_spending": tx.is_spending, "needs_review": tx.needs_review, "user_edited": tx.user_edited,
        "classification_status": tx.classification_status, "has_splits": tx.has_splits,
    }


async def _repayment_rows(session, tx: Transaction, ln: PlannedLine, *, key: str, source: str) -> list[TransactionSplit]:
    """A repayment paid with this bank transaction, broken down by the debts it pays off (oldest first).

    - the owner said what it was for ("suv olib bergan edi")  -> that spending category for the whole part
    - otherwise each paid-off debt that has a recorded purpose -> spending in that purpose (Coca-Cola ...)
    - debts without a purpose (borrowed cash)                  -> not spending, just a repayment
    - money beyond what was owed                               -> recorded as the other person now owing
    """
    person = await match_person(session, ln.person_name)
    if person is None:
        raise DebtError(f"no debts with {ln.person_name}")
    direction = debt_direction("debt_repay", tx.direction)
    result = await repay(
        session, person_id=person.id, direction=direction, amount=ln.amount, currency=tx.currency,
        paid_at=tx.transaction_datetime, description=ln.description, idempotency_key=key, allow_excess=True,
        source=source,
    )
    stated = await _category_for(session, ln, TxType.EXPENSE) if is_paid_purchase(ln, tx.direction) else None
    debts_cat = await resolve_classification(session, "Debts", "Repayment", TxType.TRANSFER)
    rows = []
    for debt, part in result.allocations:
        if stated:
            cat, sub, item, ttype = *stated, TxType.EXPENSE
        elif direction == DebtDirection.I_OWE_THEM and debt.category_id:
            cat_id, sub_id, item_id, ttype = debt.category_id, debt.subcategory_id, debt.item_id, TxType.EXPENSE
            rows.append(TransactionSplit(
                transaction_id=tx.id, kind="debt_repay", transaction_type=ttype, amount=part, category_id=cat_id,
                subcategory_id=sub_id, item_id=item_id, description=ln.description or debt.description,
                person_id=person.id, payment_key=key,
            ))
            continue
        else:
            (cat, sub), item, ttype = debts_cat, None, TxType.TRANSFER
        rows.append(TransactionSplit(
            transaction_id=tx.id, kind="debt_repay", transaction_type=ttype, amount=part, category_id=cat.id,
            subcategory_id=sub.id if sub else None, item_id=item.id if item else None,
            description=ln.description or debt.description, person_id=person.id, payment_key=key,
        ))
    if result.excess_debt is not None:
        loan = await resolve_classification(session, "Debts", "Loan", TxType.TRANSFER)
        rows.append(TransactionSplit(
            transaction_id=tx.id, kind="debt_new", transaction_type=TxType.TRANSFER, amount=result.excess,
            category_id=loan[0].id, subcategory_id=loan[1].id if loan[1] else None,
            description="paid more than owed", person_id=person.id, debt_id=result.excess_debt.id,
        ))
    return rows


async def apply_splits(session, tx_id: int, lines: list[PlannedLine], *, actor: str, key: str, source: str) -> Transaction:
    tx = (await session.scalars(select(Transaction).where(Transaction.id == tx_id).with_for_update())).first()
    if tx is None or tx.deleted_at is not None:
        raise SplitError("transaction not found")
    if sum((ln.amount for ln in lines), Decimal("0")) != tx.amount:
        raise SplitError("parts must add up to the transaction amount")

    before = _snapshot(tx)
    replaced = await _revert(session, tx, actor)
    rows: list[TransactionSplit] = []
    for i, ln in enumerate(lines):
        if ln.kind == "debt_repay":
            rows += await _repayment_rows(session, tx, ln, key=f"{key}:{i}", source=source)
            continue
        tx_type = line_type(ln, tx.direction)
        parent, child, item = await _category_for(session, ln, tx_type)
        split = TransactionSplit(
            transaction_id=tx.id, kind=ln.kind, transaction_type=tx_type, amount=ln.amount,
            category_id=parent.id, subcategory_id=child.id if child else None,
            item_id=item.id if item else None, description=ln.description, estimated=ln.estimated,
        )
        if ln.kind == "debt_new":
            debt, _ = await create_debt(
                session, person_name=ln.person_name, direction=debt_direction(ln.kind, tx.direction), amount=ln.amount,
                currency=tx.currency, description=ln.description, occurred_at=tx.transaction_datetime, source=source,
                idempotency_key=f"{key}:{i}", category=ln.category, subcategory=ln.subcategory, item=ln.item,
            )
            split.person_id, split.debt_id = debt.person_id, debt.id
        rows.append(split)
    for pos, row in enumerate(rows):
        row.position = pos
        session.add(row)
    biggest = max(rows, key=lambda r: r.amount)
    tx.transaction_type, tx.category_id = biggest.transaction_type, biggest.category_id
    tx.subcategory_id, tx.item_id = biggest.subcategory_id, biggest.item_id

    tx.has_splits = True
    tx.is_spending = any(r.transaction_type == TxType.EXPENSE for r in rows)
    tx.user_edited = True
    tx.needs_review = False
    tx.classification_status = ClassificationStatus.MANUAL
    tx.updated_at = utcnow()
    session.add(AuditLog(entity_type="transaction", entity_id=tx.id, action="explained", actor=actor,
                         changes={"before": before, "replaced_parts": replaced,
                                  "parts": [ln.to_json() for ln in lines]}))
    await session.flush()
    return tx


async def undo_explanation(session, tx_id: int, *, actor: str) -> None:
    """Remove the owner's explanation and restore the transaction exactly as it was before."""
    tx = (await session.scalars(select(Transaction).where(Transaction.id == tx_id).with_for_update())).first()
    if tx is None or not tx.has_splits:
        raise SplitError("nothing to undo")
    # Restore the state before the FIRST explanation (later answers only replaced earlier ones).
    history = (await session.scalars(
        select(AuditLog).where(AuditLog.entity_type == "transaction", AuditLog.entity_id == tx_id,
                               AuditLog.action == "explained").order_by(AuditLog.id.desc())
    )).all()
    original = next((a for a in history if not (a.changes or {}).get("before", {}).get("has_splits")), None)
    await _revert(session, tx, actor)
    before = (original.changes or {}).get("before", {}) if original else {}
    tx.item_id = None
    for k in ("transaction_type", "category_id", "subcategory_id", "item_id", "is_spending", "needs_review",
              "user_edited", "classification_status"):
        if k in before:
            setattr(tx, k, before[k])
    tx.has_splits = False
    tx.updated_at = utcnow()
    await audit(session, "transaction", tx.id, "explanation_undone", {"has_splits": (True, False)}, actor)


def describe_parts(lines: list[PlannedLine], tx_direction: str, currency: str) -> list[str]:
    from app.utils.money import format_money
    from app.utils.text import escape_html

    out = []
    for ln in lines:
        amt = format_money(ln.amount, currency)
        if ln.kind == "debt_repay":
            who = escape_html(ln.person_name or "?")
            text = f"🤝 Repaid debt to {who}" if tx_direction == TxDirection.OUT else f"🤝 {who} repaid you"
            if is_paid_purchase(ln, tx_direction):
                text += " · " + escape_html(" › ".join(x for x in (ln.category, ln.subcategory, ln.item) if x))
        elif ln.kind == "debt_new":
            who = escape_html(ln.person_name or "?")
            text = f"🤝 Lent to {who}" if tx_direction == TxDirection.OUT else f"🤝 Borrowed from {who}"
        else:
            cat = " › ".join(x for x in (ln.category, ln.subcategory, ln.item) if x)
            label = cat or KIND_LABEL[ln.kind] or "Other"
            text = f"• {escape_html(label)}"
            if ln.kind not in ("expense",) and cat:
                text += f" ({KIND_LABEL[ln.kind].lower()})" if KIND_LABEL[ln.kind] else ""
        if ln.description and ln.kind != "expense":
            text += f" — {escape_html(ln.description)}"
        out.append(f"{text}: <b>{'≈' if ln.estimated else ''}{amt}</b>")
    return out
