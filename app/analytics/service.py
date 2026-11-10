"""Analytics computed from stored transactions only (never from AI summaries).

Rules:
- income   = type 'income'; expenses = type 'expense'
- refunds are reported separately and reduce net spending, they are not income
- transfers (incl. own-card and P2P transfers), cash withdrawals and debt parts never count as income/expense
- a transaction explained by the owner as several parts ("20k food, rest taxi") is counted per part
- balance snapshots are not transactions
- every figure is per currency; currencies are never mixed
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Card, Category, Transaction, TransactionSplit
from app.models.enums import TxType
from app.utils.money import quantize

ZERO = Decimal("0.00")


def _d(v) -> Decimal:
    return quantize(Decimal(v or 0))


def _lines():
    """One row per countable line: the transaction itself, or each of its owner-defined parts."""
    T, S = Transaction, TransactionSplit
    whole = select(
        T.id.label("transaction_id"), T.transaction_type, T.direction, T.amount, T.currency,
        T.category_id, T.subcategory_id, T.transaction_datetime, T.card_id, T.merchant,
        T.description, T.deleted_at, T.needs_review, T.item_id,
    ).where(T.has_splits.is_(False))
    parts = (
        select(
            T.id, S.transaction_type, T.direction, S.amount, T.currency,
            S.category_id, S.subcategory_id, T.transaction_datetime, T.card_id, T.merchant,
            func.coalesce(S.description, T.description), T.deleted_at, literal(False), S.item_id,
        )
        .join(S, S.transaction_id == T.id)
        .where(T.has_splits.is_(True))
    )
    return union_all(whole, parts).subquery("lines")


L = _lines()


@dataclass
class Filters:
    start: datetime
    end: datetime
    currency: str = "UZS"
    card_id: int | None = None
    transaction_type: str | None = None
    category_id: int | None = None

    def conditions(self) -> list:
        conds = [
            L.c.deleted_at.is_(None),
            L.c.currency == self.currency,
            L.c.transaction_datetime >= self.start,
            L.c.transaction_datetime < self.end,
        ]
        if self.card_id is not None:
            conds.append(L.c.card_id == self.card_id)
        if self.transaction_type:
            conds.append(L.c.transaction_type == self.transaction_type)
        if self.category_id is not None:
            conds.append(or_(L.c.category_id == self.category_id, L.c.subcategory_id == self.category_id))
        return conds


    def tx_conditions(self) -> list:
        """Same filters for listing whole transactions (a split part matching counts as a match)."""
        T, S = Transaction, TransactionSplit
        conds = [
            T.deleted_at.is_(None),
            T.currency == self.currency,
            T.transaction_datetime >= self.start,
            T.transaction_datetime < self.end,
        ]
        if self.card_id is not None:
            conds.append(T.card_id == self.card_id)
        if self.transaction_type:
            conds.append(or_(
                T.transaction_type == self.transaction_type,
                select(S.id).where(S.transaction_id == T.id, S.transaction_type == self.transaction_type).exists(),
            ))
        if self.category_id is not None:
            cid = self.category_id
            conds.append(or_(
                T.category_id == cid, T.subcategory_id == cid,
                select(S.id).where(S.transaction_id == T.id, or_(S.category_id == cid, S.subcategory_id == cid)).exists(),
            ))
        return conds


def _sum_where(condition):
    return func.coalesce(func.sum(case((condition, L.c.amount), else_=0)), 0)


async def summary(session: AsyncSession, f: Filters) -> dict:
    row = (
        await session.execute(
            select(
                _sum_where(L.c.transaction_type == TxType.INCOME).label("income"),
                _sum_where(L.c.transaction_type == TxType.EXPENSE).label("expenses"),
                _sum_where(L.c.transaction_type == TxType.REFUND).label("refunds"),
                _sum_where(and_(L.c.transaction_type == TxType.TRANSFER, L.c.direction == "out")).label("transfers_out"),
                _sum_where(and_(L.c.transaction_type == TxType.TRANSFER, L.c.direction == "in")).label("transfers_in"),
                _sum_where(L.c.transaction_type == TxType.CASH_WITHDRAWAL).label("cash_withdrawals"),
                func.count(func.distinct(L.c.transaction_id)).label("count"),
                func.count(func.distinct(L.c.transaction_id)).filter(L.c.needs_review.is_(True)).label("needs_review"),
            ).where(*f.conditions())
        )
    ).one()
    income, expenses, refunds = _d(row.income), _d(row.expenses), _d(row.refunds)
    span = (min(f.end, datetime.now(f.end.tzinfo)) - f.start).total_seconds()
    days = max(1, math.ceil(span / 86400))
    net_spending = expenses - refunds
    return {
        "currency": f.currency,
        "income": income,
        "expenses": expenses,
        "refunds": refunds,
        "net_spending": net_spending,
        "net_cash_flow": income + refunds - expenses,
        "transfers_out": _d(row.transfers_out),
        "transfers_in": _d(row.transfers_in),
        "cash_withdrawals": _d(row.cash_withdrawals),
        "transaction_count": row.count,
        "needs_review": row.needs_review,
        "days": days,
        "avg_daily_spending": (net_spending / days).quantize(Decimal("0.01")),
    }


def _expense_conditions(f: Filters) -> list:
    return [*f.conditions(), L.c.transaction_type == TxType.EXPENSE]


async def by_category(session: AsyncSession, f: Filters, tx_type: str = TxType.EXPENSE) -> list[dict]:
    rows = await session.execute(
        select(Category.id, func.coalesce(Category.name, "Other"), func.sum(L.c.amount), func.count())
        .select_from(L)
        .outerjoin(Category, Category.id == L.c.category_id)
        .where(*f.conditions(), L.c.transaction_type == tx_type)
        .group_by(Category.id, Category.name)
        .order_by(func.sum(L.c.amount).desc())
    )
    return [{"category_id": cid, "name": name, "total": _d(total), "count": cnt} for cid, name, total, cnt in rows.all()]


async def by_subcategory(session: AsyncSession, f: Filters) -> list[dict]:
    Parent = aliased(Category)
    Sub = aliased(Category)
    rows = await session.execute(
        select(func.coalesce(Parent.name, "Other"), func.coalesce(Sub.name, "—"), func.sum(L.c.amount), func.count())
        .select_from(L)
        .outerjoin(Parent, Parent.id == L.c.category_id)
        .outerjoin(Sub, Sub.id == L.c.subcategory_id)
        .where(*_expense_conditions(f))
        .group_by(Parent.name, Sub.name)
        .order_by(func.sum(L.c.amount).desc())
    )
    return [{"category": c, "subcategory": s, "total": _d(t), "count": n} for c, s, t, n in rows.all()]


def _local_day(tz: ZoneInfo):
    return func.date(func.timezone(str(tz), L.c.transaction_datetime))


async def daily_spending(session: AsyncSession, f: Filters, tz: ZoneInfo) -> list[dict]:
    day = _local_day(tz)
    rows = await session.execute(
        select(day, func.sum(L.c.amount)).where(*_expense_conditions(f)).group_by(day).order_by(day)
    )
    totals = {d: _d(t) for d, t in rows.all()}
    out = []
    cur: date = f.start.astimezone(tz).date()
    last: date = (f.end - timedelta(microseconds=1)).astimezone(tz).date()
    today = datetime.now(tz).date()
    while cur <= min(last, today) and len(out) < 400:
        out.append({"date": cur.isoformat(), "total": totals.get(cur, ZERO)})
        cur += timedelta(days=1)
    return out


async def monthly_income_vs_expenses(session: AsyncSession, f: Filters, tz: ZoneInfo, months: int = 12) -> list[dict]:
    """Last `months` months ending at the filter end (ignores the filter start on purpose)."""
    end_local = (f.end - timedelta(microseconds=1)).astimezone(tz)
    y, m = end_local.year, end_local.month
    for _ in range(months - 1):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    start = datetime(y, m, 1, tzinfo=tz)
    month = func.to_char(func.timezone(str(tz), L.c.transaction_datetime), "YYYY-MM")
    conds = replace(f, start=start).conditions()
    rows = await session.execute(
        select(
            month,
            _sum_where(L.c.transaction_type == TxType.INCOME),
            _sum_where(L.c.transaction_type == TxType.EXPENSE),
            _sum_where(L.c.transaction_type == TxType.REFUND),
        ).where(*conds).group_by(month).order_by(month)
    )
    data = {mo: (_d(i), _d(e), _d(r)) for mo, i, e, r in rows.all()}
    out = []
    for _ in range(months):
        key = f"{y:04d}-{m:02d}"
        i, e, r = data.get(key, (ZERO, ZERO, ZERO))
        out.append({"month": key, "income": i, "expenses": e, "refunds": r, "net": i + r - e})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def top_merchants(session: AsyncSession, f: Filters, limit: int = 10) -> list[dict]:
    name = func.coalesce(L.c.merchant, L.c.description, "Unknown")
    rows = await session.execute(
        select(name, func.sum(L.c.amount), func.count())
        .where(*_expense_conditions(f))
        .group_by(name)
        .order_by(func.sum(L.c.amount).desc())
        .limit(limit)
    )
    return [{"merchant": n, "total": _d(t), "count": c} for n, t, c in rows.all()]


async def largest_expenses(session: AsyncSession, f: Filters, limit: int = 10) -> list[dict]:
    rows = await session.execute(
        select(L.c.transaction_id, func.coalesce(L.c.merchant, L.c.description), L.c.amount, L.c.transaction_datetime)
        .where(*_expense_conditions(f)).order_by(L.c.amount.desc()).limit(limit)
    )
    return [{"id": i, "merchant": m, "amount": _d(a), "datetime": d} for i, m, a, d in rows.all()]


async def card_comparison(session: AsyncSession, f: Filters) -> list[dict]:
    rows = await session.execute(
        select(
            Card.id, Card.bank_name, Card.card_last_four,
            _sum_where(L.c.transaction_type == TxType.EXPENSE),
            _sum_where(L.c.transaction_type == TxType.INCOME),
            func.count(func.distinct(L.c.transaction_id)),
        )
        .select_from(L)
        .join(Card, Card.id == L.c.card_id)
        .where(*f.conditions())
        .group_by(Card.id, Card.bank_name, Card.card_last_four)
        .order_by(Card.id)
    )
    return [
        {"card_id": cid, "label": f"{bank or 'HUMO'} *{last4}", "expenses": _d(e), "income": _d(i), "count": n}
        for cid, bank, last4, e, i, n in rows.all()
    ]


async def spending_trend(session: AsyncSession, f: Filters) -> dict:
    """Compare net spending in the selected period with the previous period of the same length."""
    length = f.end - f.start
    prev = Filters(f.start - length, f.start, f.currency, f.card_id, f.transaction_type, f.category_id)
    cur_s = await summary(session, f)
    prev_s = await summary(session, prev)
    change = None
    if prev_s["net_spending"] > 0:
        change = ((cur_s["net_spending"] - prev_s["net_spending"]) / prev_s["net_spending"] * 100).quantize(Decimal("0.1"))
    return {"current": cur_s["net_spending"], "previous": prev_s["net_spending"], "change_pct": change}


async def by_item(session: AsyncSession, f: Filters) -> list[dict]:
    """Spending per category › subcategory › item ("Food › Fast Food › Hot dog")."""
    Parent, Sub, Item = aliased(Category), aliased(Category), aliased(Category)
    rows = await session.execute(
        select(func.coalesce(Parent.name, "Other"), Sub.name, Item.name, func.sum(L.c.amount), func.count())
        .select_from(L)
        .outerjoin(Parent, Parent.id == L.c.category_id)
        .outerjoin(Sub, Sub.id == L.c.subcategory_id)
        .outerjoin(Item, Item.id == L.c.item_id)
        .where(*_expense_conditions(f))
        .group_by(Parent.name, Sub.name, Item.name)
        .order_by(func.sum(L.c.amount).desc())
    )
    return [{"category": c, "subcategory": s, "item": i, "total": _d(t), "count": n} for c, s, i, t, n in rows.all()]


async def app_overview(session: AsyncSession, f: Filters) -> dict:
    """Everything the Mini App overview needs for one period: totals and a category › sub › item tree."""
    s = await summary(session, f)
    cats = await by_category(session, f)
    rows = await by_item(session, f)
    for c in cats:
        subs: dict[str, dict] = {}
        for r in rows:
            if r["category"] != c["name"] or not r["subcategory"]:
                continue
            sub = subs.setdefault(r["subcategory"], {"subcategory": r["subcategory"], "total": ZERO, "count": 0, "items": []})
            sub["total"] += r["total"]
            sub["count"] += r["count"]
            if r["item"]:
                sub["items"].append({"item": r["item"], "total": r["total"], "count": r["count"]})
        c["subs"] = sorted(subs.values(), key=lambda x: -x["total"])
    return {"income": s["income"], "spent": s["net_spending"], "categories": cats}
