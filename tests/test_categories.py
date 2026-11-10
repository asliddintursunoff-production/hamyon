import asyncio

from sqlalchemy import func, select

from app.models import Category
from app.transactions.categories import get_or_create_category, resolve_classification


async def _count(sf, name_key):
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(Category).where(Category.normalized_name == name_key))


async def test_new_category_created_once_despite_spelling_variants(sf, owner_id):
    async with sf.begin() as s:
        a, _ = await resolve_classification(s, "Pets", "Pet Food", "expense")
    async with sf.begin() as s:
        b, sub_b = await resolve_classification(s, "pet", "pet food", "expense")
    async with sf.begin() as s:
        c, sub_c = await resolve_classification(s, "  PETS ", "Pet foods", "expense")
    assert a.id == b.id == c.id
    assert sub_b.id == sub_c.id
    assert a.name == "Pets"
    assert await _count(sf, "pet") == 1


async def test_existing_categories_are_reused(sf, owner_id):
    async with sf.begin() as s:
        parent, child = await resolve_classification(s, "food & drinks", "restaurant", "expense")
    assert parent.name == "Food" and child.name == "Restaurants"
    async with sf.begin() as s:
        parent, child = await resolve_classification(s, "Transport", "Taksi", "expense")
    assert child.name == "Taxi"


async def test_subcategory_returned_as_category_maps_to_parent(sf, owner_id):
    async with sf.begin() as s:
        parent, child = await resolve_classification(s, "Taxi", None, "expense")
    assert parent.name == "Transport" and child.name == "Taxi"


async def test_typo_is_merged(sf, owner_id):
    async with sf.begin() as s:
        parent, _ = await resolve_classification(s, "Entertainmnet", None, "expense")
    assert parent.name == "Entertainment"


async def test_concurrent_creation_has_no_duplicates(sf, owner_id):
    async def create():
        async with sf.begin() as s:
            return (await get_or_create_category(s, "Subscriptions")).id

    ids = await asyncio.gather(*(create() for _ in range(8)))
    assert len(set(ids)) == 1
    assert await _count(sf, "subscription") == 1


async def test_invalid_names_fall_back_to_other(sf, owner_id):
    async with sf.begin() as s:
        parent, _ = await resolve_classification(s, "<script>alert(1)</script>", None, "expense")
    assert parent.name == "Other"
