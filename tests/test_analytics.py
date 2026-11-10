from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.analytics import service as an
from app.models import Transaction
from app.models.enums import ClassificationStatus, TxSource
from app.transactions.categories import resolve_classification
from app.utils.timeutil import period_range

TZ = ZoneInfo("Asia/Tashkent")
D = Decimal


async def add(sf, ttype, amount, when, *, direction=None, currency="UZS", cat=("Other", None), merchant=None, card_id=None):
    async with sf.begin() as s:
        parent, child = await resolve_classification(s, cat[0], cat[1], ttype)
        s.add(Transaction(
            transaction_type=ttype, direction=direction or ("in" if ttype in ("income", "refund") else "out"),
            amount=D(amount), currency=currency, transaction_datetime=when, source=TxSource.DASHBOARD,
            category_id=parent.id, subcategory_id=child.id if child else None, merchant=merchant,
            is_spending=ttype == "expense", classification_status=ClassificationStatus.MANUAL, card_id=card_id,
        ))


def local(d, h=12, m=0):
    return datetime(2026, 11, d, h, m, tzinfo=TZ)


async def test_summary_rules(sf, owner_id):
    await add(sf, "income", "1000000", local(1))
    await add(sf, "expense", "16213", local(6), cat=("Transport", "Taxi"), merchant="WB TAXI")
    await add(sf, "expense", "50000", local(7), cat=("Food", "Cafes"), merchant="CAFE")
    await add(sf, "refund", "10000", local(8))
    await add(sf, "transfer", "100300", local(6))                  # P2P out
    await add(sf, "transfer", "100300", local(6), direction="in")  # own-card transfer in
    await add(sf, "cash_withdrawal", "200000", local(3))
    await add(sf, "expense", "99", local(6), currency="USD")       # other currency must not be mixed
    f = an.Filters(datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 12, 1, tzinfo=TZ), "UZS")
    async with sf() as s:
        summ = await an.summary(s, f)
        cats = await an.by_category(s, f)
        subs = await an.by_subcategory(s, f)
        merchants = await an.top_merchants(s, f)
        monthly = await an.monthly_income_vs_expenses(s, f, TZ)
    assert summ["income"] == D("1000000.00")
    assert summ["expenses"] == D("66213.00")
    assert summ["refunds"] == D("10000.00")
    assert summ["net_spending"] == D("56213.00")
    assert summ["net_cash_flow"] == D("943787.00")
    assert summ["transfers_out"] == D("100300.00") and summ["transfers_in"] == D("100300.00")
    assert summ["cash_withdrawals"] == D("200000.00")
    assert [c["name"] for c in cats] == ["Food", "Transport"]
    assert {(r["category"], r["subcategory"]) for r in subs} == {("Food", "Cafes"), ("Transport", "Taxi")}
    assert merchants[0]["merchant"] == "CAFE"
    nov = next(m for m in monthly if m["month"] == "2026-11")
    assert nov["income"] == D("1000000.00") and nov["expenses"] == D("66213.00")


async def test_daily_buckets_use_local_timezone(sf, owner_id):
    # 23:30 local on Nov 5 is 18:30 UTC Nov 5; 00:30 local Nov 6 is 19:30 UTC Nov 5 -> must land on Nov 6
    await add(sf, "expense", "100", local(5, 23, 30))
    await add(sf, "expense", "200", local(6, 0, 30))
    f = an.Filters(datetime(2026, 11, 5, tzinfo=TZ), datetime(2026, 11, 7, tzinfo=TZ), "UZS")
    async with sf() as s:
        daily = {d["date"]: d["total"] for d in await an.daily_spending(s, f, TZ)}
    assert daily["2026-11-05"] == D("100") and daily["2026-11-06"] == D("200")


def test_period_ranges_are_timezone_aware():
    now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)  # 01:00 on Nov 10 in Tashkent
    start, end = period_range("today", TZ, now)
    assert start == datetime(2026, 11, 10, tzinfo=TZ) and end == datetime(2026, 11, 11, tzinfo=TZ)
    start, end = period_range("last_month", TZ, now)
    assert start == datetime(2026, 10, 1, tzinfo=TZ) and end == datetime(2026, 11, 1, tzinfo=TZ)
    start, _ = period_range("week", TZ, now)
    assert start.weekday() == 0
