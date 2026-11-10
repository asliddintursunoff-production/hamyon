"""Merchant memory: avoid AI calls for merchants we already know, and honour owner corrections."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Merchant
from app.utils.text import normalize_merchant

P2P_MARKERS = (
    "P2P", "H2H", "H2U", "U2H", "CARD2CARD", "C2C", "PEREVOD", "O'TKAZMA", "OTKAZMA",
    "HUMO TO HUMO", "HUMO TO VISA", "HUMO2VISA", "VISA TO HUMO", "TO VISA", "UZCARD TO HUMO",
)
# Money moved between the owner's own accounts (currency conversion, own e-wallet top-up).
OWN_ACCOUNT_MARKERS = ("KONVERSIYA", "KONVERTATSIYA", "CONVERSION", "NA KOSHELEK", "KOSHELEK", "HAMYON")


def looks_like_own_account(merchant: str | None) -> bool:
    m = normalize_merchant(merchant or "")
    return any(marker in m for marker in OWN_ACCOUNT_MARKERS)


def looks_like_p2p(merchant: str | None) -> bool:
    m = normalize_merchant(merchant or "")
    return looks_like_own_account(m) or any(marker in m for marker in P2P_MARKERS)


@dataclass
class RuleDecision:
    kind: str  # "use_rule" | "known_unknown" | "ask_ai"
    merchant: Merchant | None


def decide(merchant: Merchant | None, now: datetime, negative_cache_days: int, min_confidence: float) -> RuleDecision:
    if merchant is None:
        return RuleDecision("ask_ai", None)
    if merchant.confirmed and merchant.category_id:
        return RuleDecision("use_rule", merchant)
    if merchant.category_id and merchant.transaction_type and (merchant.ai_confidence or 0) >= Decimal(str(min_confidence)):
        return RuleDecision("use_rule", merchant)
    # Gemini recently could not identify this merchant: don't spend quota again yet.
    if merchant.category_id is None and merchant.last_ai_at and now - merchant.last_ai_at < timedelta(days=negative_cache_days):
        return RuleDecision("known_unknown", merchant)
    return RuleDecision("ask_ai", merchant)


def _one(key: str, direction: str):
    return select(Merchant).where(Merchant.normalized_name == key, Merchant.direction == direction)


async def find_merchant(session: AsyncSession, raw_name: str | None, direction: str) -> Merchant | None:
    if not raw_name:
        return None
    return (await session.scalars(_one(normalize_merchant(raw_name), direction))).first()


async def ensure_merchant(session: AsyncSession, raw_name: str, direction: str) -> Merchant:
    key = normalize_merchant(raw_name)
    await session.execute(
        insert(Merchant)
        .values(normalized_name=key, direction=direction, display_name=raw_name.strip()[:255])
        .on_conflict_do_nothing()
    )
    return (await session.scalars(_one(key, direction))).one()


async def remember_ai_result(
    session: AsyncSession,
    raw_name: str,
    *,
    direction: str,
    category_id: int | None,
    subcategory_id: int | None,
    transaction_type: str | None,
    is_spending: bool | None,
    description: str | None,
    confidence: Decimal | None,
    now: datetime,
) -> Merchant:
    """Store what AI learned. Never overwrites an owner-confirmed rule."""
    key = normalize_merchant(raw_name)
    values = dict(
        normalized_name=key,
        direction=direction,
        display_name=raw_name.strip()[:255],
        category_id=category_id,
        subcategory_id=subcategory_id,
        transaction_type=transaction_type,
        is_spending=is_spending,
        description=description,
        ai_confidence=confidence,
        last_ai_at=now,
    )
    stmt = insert(Merchant).values(**values)
    update_cols = {k: stmt.excluded[k] for k in values if k not in ("normalized_name", "direction", "display_name")}
    update_cols["updated_at"] = now
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Merchant.normalized_name, Merchant.direction],
            set_=update_cols,
            where=Merchant.confirmed.is_(False),
        )
    )
    return (await session.scalars(_one(key, direction))).one()


async def set_owner_rule(
    session: AsyncSession,
    raw_name: str,
    *,
    direction: str,
    category_id: int,
    subcategory_id: int | None,
    transaction_type: str,
    is_spending: bool,
    now: datetime,
) -> Merchant:
    """Owner correction: becomes the preferred classification for all future transactions."""
    key = normalize_merchant(raw_name)
    values = dict(
        normalized_name=key,
        direction=direction,
        display_name=raw_name.strip()[:255],
        category_id=category_id,
        subcategory_id=subcategory_id,
        transaction_type=transaction_type,
        is_spending=is_spending,
        confirmed=True,
    )
    stmt = insert(Merchant).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Merchant.normalized_name, Merchant.direction],
            set_={
                "category_id": stmt.excluded.category_id,
                "subcategory_id": stmt.excluded.subcategory_id,
                "transaction_type": stmt.excluded.transaction_type,
                "is_spending": stmt.excluded.is_spending,
                "confirmed": True,
                "updated_at": now,
            },
        )
    )
    return (await session.scalars(_one(key, direction))).one()
