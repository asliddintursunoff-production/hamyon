from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import selectinload

from app.analytics import service as analytics
from app.api.auth import require_owner, require_owner_write
from app.api.serialize import json_response
from app.cards.service import list_cards, totals_by_currency
from app.database.seed import get_owner
from app.debts.service import DebtError, cancel_debt, cancel_payment, debt_overview, person_history
from app.models import Card, Category, Transaction, TransactionSplit
from app.models.enums import TxType
from app.telegram.balance_sync import BalanceRefreshError
from app.transactions.service import TransactionNotFound, update_transaction_manual

router = APIRouter(prefix="/api", dependencies=[Depends(require_owner)])


def sf(request: Request):
    return request.app.state.sf


async def owner_tz(request: Request) -> ZoneInfo:
    async with sf(request)() as s:
        owner = await get_owner(s)
    try:
        return ZoneInfo(owner.timezone)
    except Exception:
        return request.app.state.settings.tz


async def filters_dep(
    request: Request,
    period: Literal["today", "week", "month", "last_month", "year", "custom"] = "month",
    start: date | None = None,
    end: date | None = None,
    card_id: int | None = None,
    transaction_type: str | None = Query(None, alias="type"),
    category_id: int | None = None,
    currency: str | None = None,
) -> tuple[analytics.Filters, ZoneInfo]:
    from app.utils.timeutil import period_range

    tz = await owner_tz(request)
    if period == "custom":
        if not start or not end or end < start or (end - start).days > 3660:
            raise HTTPException(400, "custom period needs valid start and end dates")
        s_dt = datetime.combine(start, time.min, tzinfo=tz)
        e_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    else:
        s_dt, e_dt = period_range(period, tz)
    if transaction_type and transaction_type not in {t.value for t in TxType}:
        raise HTTPException(400, "invalid type")
    cur = (currency or request.app.state.settings.currency).upper()[:3]
    return analytics.Filters(s_dt, e_dt, cur, card_id, transaction_type, category_id), tz


@router.get("/overview")
async def overview(request: Request, fz=Depends(filters_dep)):
    f, tz = fz
    async with sf(request)() as s:
        summ = await analytics.summary(s, f)
        trend = await analytics.spending_trend(s, f)
        cards = await list_cards(s)
        owed_to_me, i_owe, net = (await debt_overview(s)).totals(f.currency)
    return json_response({
        "period": {"start": f.start, "end": f.end, "timezone": str(tz)},
        "summary": summ,
        "trend": trend,
        "cards": [
            {"id": c.id, "label": c.label, "bank_name": c.bank_name, "last4": c.card_last_four, "currency": c.currency,
             "balance": c.current_balance, "synced_at": c.last_balance_sync_at}
            for c in cards
        ],
        "cards_total": totals_by_currency(cards),
        "debts": {"owed_to_me": owed_to_me, "i_owe": i_owe, "net": net},
    })


@router.get("/app/overview")
async def app_overview(request: Request, fz=Depends(filters_dep)):
    """Lightweight data for the Mini App overview (one period)."""
    f, _ = fz
    async with sf(request)() as s:
        data = await analytics.app_overview(s, f)
    return json_response(data)


@router.get("/analytics")
async def analytics_data(request: Request, fz=Depends(filters_dep)):
    f, tz = fz
    async with sf(request)() as s:
        data = {
            "by_category": await analytics.by_category(s, f),
            "income_by_category": await analytics.by_category(s, f, TxType.INCOME),
            "by_subcategory": await analytics.by_subcategory(s, f),
            "daily": await analytics.daily_spending(s, f, tz),
            "monthly": await analytics.monthly_income_vs_expenses(s, f, tz),
            "top_merchants": await analytics.top_merchants(s, f),
            "largest": await analytics.largest_expenses(s, f),
            "cards": await analytics.card_comparison(s, f),
        }
    return json_response(data)


SORT_COLUMNS = {
    "datetime": Transaction.transaction_datetime,
    "amount": Transaction.amount,
    "merchant": Transaction.merchant,
    "type": Transaction.transaction_type,
}


@router.get("/transactions")
async def transactions(
    request: Request,
    fz=Depends(filters_dep),
    q: str | None = Query(None, max_length=100),
    needs_review: bool | None = None,
    sort: Literal["datetime", "amount", "merchant", "type"] = "datetime",
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    f, tz = fz
    conds = f.tx_conditions()
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        conds.append(or_(Transaction.merchant.ilike(like), Transaction.description.ilike(like)))
    if needs_review is not None:
        conds.append(Transaction.needs_review.is_(needs_review))
    col = SORT_COLUMNS[sort]
    async with sf(request)() as s:
        total = await s.scalar(select(func.count()).select_from(Transaction).where(*conds))
        rows = (
            await s.scalars(
                select(Transaction)
                .options(
                    selectinload(Transaction.category), selectinload(Transaction.subcategory), selectinload(Transaction.card),
                    selectinload(Transaction.splits).selectinload(TransactionSplit.category),
                    selectinload(Transaction.splits).selectinload(TransactionSplit.subcategory),
                    selectinload(Transaction.splits).selectinload(TransactionSplit.item),
                    selectinload(Transaction.item),
                )
                .where(*conds)
                .order_by((desc if order == "desc" else asc)(col), desc(Transaction.id))
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    return json_response({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": t.id, "datetime": t.transaction_datetime, "merchant": t.merchant, "description": t.description,
                "amount": t.amount, "currency": t.currency, "direction": t.direction, "type": t.transaction_type,
                "category_id": t.category_id, "category": t.category.name if t.category else None,
                "subcategory_id": t.subcategory_id, "subcategory": t.subcategory.name if t.subcategory else None,
                "item": t.item.name if t.item else None,
                "card": t.card.label if t.card else None, "source": t.source,
                "ai_confidence": t.ai_confidence, "classification": t.classification_status,
                "needs_review": t.needs_review, "can_reclassify": bool(t.original_message_id and not t.user_edited),
                "splits": [
                    {"amount": p.amount, "type": p.transaction_type, "kind": p.kind,
                     "category": p.category.name if p.category else None,
                     "subcategory": p.subcategory.name if p.subcategory else None,
                     "item": p.item.name if p.item else None, "description": p.description, "estimated": p.estimated}
                    for p in t.splits
                ],
            }
            for t in rows
        ],
    })


class TransactionPatch(BaseModel):
    category_id: int | None = None
    subcategory_id: int | None = None
    transaction_type: str | None = None
    description: str | None = Field(None, max_length=500)
    apply_to_merchant: bool = False
    mark_reviewed: bool = False


@router.patch("/transactions/{tx_id}", dependencies=[Depends(require_owner_write)])
async def patch_transaction(tx_id: int, body: TransactionPatch, request: Request):
    try:
        async with sf(request).begin() as s:
            tx = await update_transaction_manual(
                s, tx_id, actor="owner:dashboard", category_id=body.category_id, subcategory_id=body.subcategory_id,
                transaction_type=body.transaction_type, description=body.description, apply_to_merchant=body.apply_to_merchant,
            )
            result = {"id": tx.id, "ok": True}
    except TransactionNotFound:
        raise HTTPException(404, "transaction not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return json_response(result)


@router.post("/transactions/{tx_id}/reclassify", dependencies=[Depends(require_owner_write)])
async def reclassify(tx_id: int, request: Request):
    ok = await request.app.state.processor.reclassify(tx_id)
    if not ok:
        raise HTTPException(400, "cannot reclassify this transaction")
    return json_response({"ok": True})


@router.get("/categories")
async def categories(request: Request):
    async with sf(request)() as s:
        rows = (await s.scalars(select(Category).order_by(Category.name))).all()
    parents = [c for c in rows if c.parent_id is None]
    return json_response([
        {"id": p.id, "name": p.name, "type": p.category_type,
         "children": [{"id": c.id, "name": c.name} for c in rows if c.parent_id == p.id]}
        for p in parents
    ])


@router.get("/cards")
async def cards(request: Request):
    async with sf(request)() as s:
        items = await list_cards(s)
    return json_response([
        {"id": c.id, "label": c.label, "balance": c.current_balance, "currency": c.currency, "synced_at": c.last_balance_sync_at}
        for c in items
    ])


@router.post("/cards/refresh", dependencies=[Depends(require_owner_write)])
async def refresh_cards(request: Request):
    balance = request.app.state.balance
    try:
        result = await balance.refresh("dashboard")
    except BalanceRefreshError as exc:
        return json_response({"ok": False, "error": str(exc)}, status_code=503)
    return json_response({"ok": True, "completed_at": result.completed_at})


@router.get("/debts")
async def debts(request: Request):
    cur = request.app.state.settings.currency
    async with sf(request)() as s:
        ov = await debt_overview(s)
    owed_to_me, i_owe, net = ov.totals(cur)
    return json_response({
        "currency": cur,
        "totals": {"owed_to_me": owed_to_me, "i_owe": i_owe, "net": net},
        "people": [
            {"id": p.person_id, "name": p.name, "currency": p.currency, "they_owe_me": p.they_owe_me,
             "i_owe_them": p.i_owe_them, "net": p.net}
            for p in ov.people
        ],
    })


@router.get("/debts/people/{person_id}")
async def debt_person(person_id: int, request: Request):
    async with sf(request)() as s:
        try:
            h = await person_history(s, person_id)
        except DebtError:
            raise HTTPException(404, "not found")
    p = h["person"]
    return json_response({"person": {"id": p.id, "name": p.name, "notes": p.notes}, "events": h["events"]})


@router.post("/debts/{debt_id}/cancel", dependencies=[Depends(require_owner_write)])
async def cancel_debt_api(debt_id: int, request: Request):
    try:
        async with sf(request).begin() as s:
            await cancel_debt(s, debt_id, reason="cancelled from dashboard", actor="owner:dashboard")
    except DebtError as exc:
        raise HTTPException(400, str(exc))
    return json_response({"ok": True})


@router.post("/debt-payments/{payment_id}/cancel", dependencies=[Depends(require_owner_write)])
async def cancel_payment_api(payment_id: int, request: Request):
    try:
        async with sf(request).begin() as s:
            await cancel_payment(s, payment_id, reason="cancelled from dashboard", actor="owner:dashboard")
    except DebtError as exc:
        raise HTTPException(400, str(exc))
    return json_response({"ok": True})
