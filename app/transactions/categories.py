"""Category tree with duplicate-safe creation.

`normalized_name` stores a canonical key (lowercase, '&'->'and', synonyms, singular form), and the
database enforces uniqueness of (parent, key). "Restaurant", "restaurants " and "RESTAURANTS" therefore
always resolve to the same row, even under concurrent creation.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category
from app.models.enums import CategoryType, TxType
from app.utils.text import normalize_name, similarity, singular

SEED_CATEGORIES: list[tuple[str, CategoryType, list[str]]] = [
    ("Food", CategoryType.EXPENSE, ["Restaurants", "Fast Food", "Cafes", "Food Delivery", "Snacks"]),
    ("Transport", CategoryType.EXPENSE, ["Taxi", "Public Transport", "Fuel"]),
    ("Shopping", CategoryType.EXPENSE, ["Groceries", "Electronics", "Clothing"]),
    ("Housing", CategoryType.EXPENSE, []),
    ("Bills and Utilities", CategoryType.EXPENSE, ["Mobile", "Internet", "Utilities"]),
    ("Entertainment", CategoryType.EXPENSE, []),
    ("Health", CategoryType.EXPENSE, []),
    ("Education", CategoryType.EXPENSE, []),
    ("Transfers", CategoryType.TRANSFER, ["P2P", "Own Cards", "Sent to Others"]),
    ("Debts", CategoryType.TRANSFER, ["Repayment", "Loan"]),
    ("Income", CategoryType.INCOME, ["Salary", "Transfers Received", "Card Top-up", "Cashback"]),
    ("Refunds", CategoryType.INCOME, []),
    ("Cash", CategoryType.ANY, ["ATM Withdrawal"]),
    ("Other", CategoryType.ANY, ["Unclassified"]),
]

OTHER = "Other"
UNCLASSIFIED = "Unclassified"

_SYNONYMS = {
    "taksi": "taxi",
    "cab": "taxi",
    "food and drink": "food",
    "food and drinks": "food",
    "restaurant and cafe": "restaurant",
    "dining": "restaurant",
    "cafe": "cafe",
    "coffee": "cafe",
    "coffee shop": "cafe",
    "supermarket": "grocery",
    "groceries": "grocery",
    "grocery store": "grocery",
    "transfer": "transfer",
    "p2p transfer": "p2p",
    "bill": "bills and utility",
    "utilities and bills": "bills and utility",
    "bills": "bills and utility",
    "uncategorized": "unclassified",
    "unknown": "unclassified",
    "misc": "other",
    "miscellaneous": "other",
    "healthcare": "health",
    "medical": "health",
    "pharmacy": "pharmacy",
    "salary and wage": "salary",
    "wage": "salary",
    "public transit": "public transport",
    "gas": "fuel",
    "petrol": "fuel",
    "atm": "atm withdrawal",
    "cash withdrawal": "atm withdrawal",
    "refund": "refund",
}

_VALID_NAME_RE = re.compile(r"^[\w][\w &/'.,()+-]{0,58}$", re.UNICODE)


def canonical_key(name: str) -> str:
    s = normalize_name(name).replace("&", " and ")
    s = re.sub(r"\s+", " ", s).strip()
    s = _SYNONYMS.get(s, s)
    s = " ".join(singular(w) for w in s.split(" "))
    return _SYNONYMS.get(s, s)


def clean_display_name(name: str) -> str | None:
    name = " ".join((name or "").split()).strip(" .,-")
    if not name or not _VALID_NAME_RE.match(name):
        return None
    if name.islower():
        name = name.title()
    return name


def category_type_for(tx_type: str) -> CategoryType:
    return {
        TxType.INCOME: CategoryType.INCOME,
        TxType.REFUND: CategoryType.INCOME,
        TxType.TRANSFER: CategoryType.TRANSFER,
        TxType.EXPENSE: CategoryType.EXPENSE,
    }.get(tx_type, CategoryType.ANY)


async def _siblings(session: AsyncSession, parent_id: int | None) -> list[Category]:
    stmt = select(Category).where(
        Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
    )
    return list((await session.scalars(stmt)).all())


def _best_match(key: str, candidates: list[Category]) -> Category | None:
    for c in candidates:
        if c.normalized_name == key:
            return c
    best, best_score = None, 0.0
    for c in candidates:
        score = similarity(key, c.normalized_name)
        if score > best_score:
            best, best_score = c, score
    # Only very close spellings are merged ("restarant" -> "restaurant"); short words need exact match.
    if best is not None and best_score >= 0.88 and len(key) >= 5:
        return best
    return None


async def get_or_create_category(
    session: AsyncSession,
    name: str,
    *,
    parent: Category | None = None,
    category_type: CategoryType = CategoryType.EXPENSE,
) -> Category:
    display = clean_display_name(name)
    if display is None:
        raise ValueError(f"invalid category name: {name!r}")
    key = canonical_key(display)
    parent_id = parent.id if parent else None

    existing = _best_match(key, await _siblings(session, parent_id))
    if existing:
        return existing

    ctype = parent.category_type if parent is not None else category_type
    await session.execute(
        insert(Category)
        .values(name=display, normalized_name=key, parent_id=parent_id, category_type=ctype)
        .on_conflict_do_nothing()
    )
    stmt = select(Category).where(
        Category.normalized_name == key,
        Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id,
    )
    return (await session.scalars(stmt)).one()


async def find_subcategory_anywhere(session: AsyncSession, name: str) -> Category | None:
    key = canonical_key(name)
    stmt = select(Category).where(Category.normalized_name == key, Category.parent_id.is_not(None))
    return (await session.scalars(stmt)).first()


async def resolve_classification(
    session: AsyncSession, category: str | None, subcategory: str | None, tx_type: str
) -> tuple[Category, Category | None]:
    """Map AI/user-provided names to (parent, child) rows, creating genuinely new ones."""
    ctype = category_type_for(tx_type)
    cat_name = clean_display_name(category or "") or OTHER

    # AI sometimes returns a subcategory as the category ("Taxi"); map it to its parent.
    top_level = await _siblings(session, None)
    parent = _best_match(canonical_key(cat_name), top_level)
    if parent is None:
        as_child = await find_subcategory_anywhere(session, cat_name)
        if as_child is not None:
            parent = await session.get(Category, as_child.parent_id)
            if not subcategory:
                return parent, as_child
    if parent is None:
        parent = await get_or_create_category(session, cat_name, category_type=ctype)

    child = None
    sub_name = clean_display_name(subcategory or "")
    if sub_name and canonical_key(sub_name) != parent.normalized_name:
        child = await get_or_create_category(session, sub_name, parent=parent)
    return parent, child


async def resolve_full(
    session: AsyncSession, category: str | None, subcategory: str | None, item: str | None, tx_type: str
) -> tuple[Category, Category | None, Category | None]:
    """Category › subcategory › item ("Food › Fast Food › Hot dog"). Without a subcategory the item
    itself becomes the subcategory. Spelling variants of an item merge into one row."""
    parent, child = await resolve_classification(session, category, subcategory, tx_type)
    name = clean_display_name(item or "")
    if not name:
        return parent, child, None
    key = canonical_key(name)
    if child is None:
        if key == parent.normalized_name:
            return parent, None, None
        return parent, await get_or_create_category(session, name, parent=parent), None
    if key in (child.normalized_name, parent.normalized_name):
        return parent, child, None
    return parent, child, await get_or_create_category(session, name, parent=child)


async def unclassified(session: AsyncSession) -> tuple[Category, Category]:
    other = await get_or_create_category(session, OTHER, category_type=CategoryType.ANY)
    sub = await get_or_create_category(session, UNCLASSIFIED, parent=other)
    return other, sub


async def seed_categories(session: AsyncSession) -> None:
    for name, ctype, children in SEED_CATEGORIES:
        parent = await get_or_create_category(session, name, category_type=ctype)
        for child in children:
            await get_or_create_category(session, child, parent=parent)


async def categories_prompt_text(session: AsyncSession) -> str:
    rows = list((await session.scalars(select(Category).order_by(Category.parent_id.nulls_first(), Category.name))).all())
    parents = [c for c in rows if c.parent_id is None]
    lines = []
    for p in parents:
        kids = [c.name for c in rows if c.parent_id == p.id]
        lines.append(f"- {p.name}: {', '.join(kids) if kids else '(no subcategories)'}")
    return "\n".join(lines)
