"""Incoming money: 'Karta hisobi o'zgardi' notices, duplicates, P2P income, own-card transfers, offline parser."""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.ai.gemini import AIQuotaExhausted
from app.assistant.offline import parse_offline
from app.models import Category, TelegramMessage, Transaction
from app.models.enums import ClassificationStatus, MsgKind, MsgStatus, TxDirection, TxType
from app.transactions.maintenance import upgrade_existing_data
from app.transactions.parser import detect_kind, parse_transaction
from app.transactions.pipeline import MessageProcessor
from app.utils.timeutil import utcnow
from tests.conftest import FakeAI, tg_date

TZ = ZoneInfo("Asia/Tashkent")
CHAT = 777

CHANGED = """ℹ️ Karta hisobi o'zgardi
💸 4.337.505,00 UZS
💳 HUMO-CARD *5592
🕘 15:33 05.11.2026"""

TOPUP = """🎉 To'ldirish
➕ 3.000.000,00 UZS
📍 IYM HUMO P2P KOMISSI
💳 HUMOCARD *5592
🕓 09:25 01.11.2026
💰 7.578.779,00 UZS"""

REFUND_DETAILED = """🎉 To'ldirish
➕ 24.000,00 UZS
📍 oplata
💳 HUMOCARD *5592
🕓 22:25 08.10.2026
💰 1.000.000,00 UZS"""

REFUND_CHANGED = """ℹ️ Karta hisobi o'zgardi
💸 24.000,00 UZS
💳 HUMO-CARD *5592
🕘 22:25 08.10.2026"""


def test_parse_card_changed_notice():
    assert detect_kind(CHANGED) == MsgKind.TRANSACTION
    p = parse_transaction(CHANGED, TZ)
    assert p.direction == TxDirection.IN and p.amount == Decimal("4337505.00") and p.generic
    assert p.card_last_four == "5592" and p.merchant is None
    assert p.operation_title == "Karta hisobi o'zgardi"
    assert not parse_transaction(TOPUP, TZ).generic
    assert detect_kind("Kutib turing...") == MsgKind.UNKNOWN


async def run(sf, settings, owner_id, *msgs, ai=None):
    ai = ai or FakeAI()  # no responder: any AI call raises AIUnavailable
    proc = MessageProcessor(sf, ai, settings, owner_id)
    outs = []
    for i, (text, date) in enumerate(msgs, start=1):
        r = await proc.ingest(chat_id=CHAT, message_id=i, sender_id=CHAT, text=text, date=date)
        outs.append(await proc.process(r.row_id))
    return proc, ai, outs


async def live_txs(sf):
    async with sf() as s:
        return list((await s.scalars(select(Transaction).where(Transaction.deleted_at.is_(None)))).all())


async def cat_names(sf, tx):
    async with sf() as s:
        return (await s.get(Category, tx.category_id)).name, (await s.get(Category, tx.subcategory_id)).name


async def test_card_changed_becomes_income_without_ai(sf, settings, owner_id):
    _, ai, outs = await run(sf, settings, owner_id, (CHANGED, tg_date(d=5, h=10, mi=33)))
    assert outs[0].status == MsgStatus.SAVED and ai.calls == []
    [tx] = await live_txs(sf)
    assert tx.transaction_type == TxType.INCOME and tx.amount == Decimal("4337505.00") and not tx.needs_review
    assert await cat_names(sf, tx) == ("Income", "Card Top-up")


async def test_incoming_p2p_is_income_not_transfer(sf, settings, owner_id):
    _, ai, _ = await run(sf, settings, owner_id, (TOPUP, tg_date(d=1, h=4, mi=25)))
    [tx] = await live_txs(sf)
    assert tx.transaction_type == TxType.INCOME and tx.classification_status == ClassificationStatus.RULE
    assert await cat_names(sf, tx) == ("Income", "Transfers Received")
    assert ai.calls == []


@pytest.mark.parametrize("order", ["detailed_first", "notice_first"])
async def test_duplicate_notice_counted_once(sf, settings, owner_id, order):
    d = tg_date(mo=10, d=8, h=17, mi=25)
    msgs = [(REFUND_DETAILED, d), (REFUND_CHANGED, d)]
    if order == "notice_first":
        msgs.reverse()
    await run(sf, settings, owner_id, *msgs, ai=FakeAI(lambda p, a: {
        "transaction_type": "refund", "category": "Refunds", "is_spending": False, "confidence": 0.9}))
    live = await live_txs(sf)
    assert len(live) == 1 and live[0].merchant == "oplata"  # the detailed one wins
    async with sf() as s:
        total = await s.scalar(select(func.count()).select_from(Transaction))
    assert total == (1 if order == "detailed_first" else 2)  # replaced notice is kept, soft-deleted


async def test_transfer_between_own_cards(sf, settings, owner_id):
    out_msg = """💸 To'lov
➖ 101.000,00 UZS
📍 TBC HUMO P2P>TASHKEN
💳 HUMOCARD *3798
🕓 12:00 06.11.2026
💰 500,00 UZS"""
    in_msg = """🎉 To'ldirish
➕ 100.000,00 UZS
📍 Octo Mobile P2P H2H>
💳 HUMOCARD *5592
🕓 12:01 06.11.2026
💰 7.000.000,00 UZS"""
    await run(sf, settings, owner_id, (out_msg, tg_date(d=6, h=7, mi=0)), (in_msg, tg_date(d=6, h=7, mi=1)))
    txs = await live_txs(sf)
    assert len(txs) == 2
    for t in txs:
        assert t.transaction_type == TxType.TRANSFER and not t.is_spending
        assert await cat_names(sf, t) == ("Transfers", "Own Cards")


async def test_upkeep_fixes_existing_data(sf, settings, owner_id):
    proc = MessageProcessor(sf, FakeAI(), settings, owner_id)
    # simulate data stored by the old version: ignored notice + incoming P2P saved as transfer
    async with sf.begin() as s:
        s.add(TelegramMessage(chat_id=CHAT, telegram_message_id=50, message_text=CHANGED, message_date=tg_date(d=5),
                              kind=MsgKind.UNKNOWN, processing_status=MsgStatus.IGNORED))
    r = await proc.ingest(chat_id=CHAT, message_id=51, sender_id=CHAT, text=TOPUP, date=tg_date(d=1))
    await proc.process(r.row_id)
    async with sf.begin() as s:
        tx = (await s.scalars(select(Transaction))).one()
        tx.transaction_type = TxType.TRANSFER
    await upgrade_existing_data(sf)
    await upgrade_existing_data(sf)  # idempotent
    async with sf() as s:
        msg = (await s.scalars(select(TelegramMessage).where(TelegramMessage.telegram_message_id == 50))).one()
        tx = (await s.scalars(select(Transaction))).one()
    assert msg.kind == MsgKind.TRANSACTION and msg.processing_status == MsgStatus.RECEIVED
    assert tx.transaction_type == TxType.INCOME


async def test_quota_exhaustion_waits_without_burning_attempts(sf, settings, owner_id):
    import time

    expense = """💸 To'lov
➖ 16.213,00 UZS
📍 OOO WB TAXI OPLATA>T
💳 HUMOCARD *5592
🕓 09:59 06.11.2026
💰 13.272.858,60 UZS"""
    retry_at = time.time() + 6 * 3600
    _, _, outs = await run(sf, settings, owner_id, (expense, tg_date()), ai=FakeAI(lambda p, a: AIQuotaExhausted(retry_at)))
    assert outs[0].status == MsgStatus.AWAITING_AI
    async with sf() as s:
        msg = (await s.scalars(select(TelegramMessage))).one()
    assert msg.attempts == 0
    assert msg.next_attempt_at > utcnow() + timedelta(hours=5)
    [tx] = await live_txs(sf)
    assert tx.amount == Decimal("16213.00")  # still recorded


@pytest.mark.parametrize("text, intent, name, amount, direction", [
    ("19 000 Orzubekdan qarz bo'ldim", "create_debt", "Orzubek", "19000", "i_owe_them"),
    ("Orzubek mendan 20 ming qarzi bo'ldi", "create_debt", "Orzubek", "20000", "they_owe_me"),
    ("Aliga 30 ming qarz berdim", "create_debt", "Ali", "30000", "they_owe_me"),
    ("Ali 20 ming qaytardi", "repay_debt", "Ali", "20000", "they_owe_me"),
    ("Aliga 20 ming qaytardim", "repay_debt", "Ali", "20000", "i_owe_them"),
    ("I borrowed 50,000 UZS from Ali", "create_debt", "Ali", "50000", "i_owe_them"),
    ("Bob owes me 100,000 UZS", "create_debt", "Bob", "100000", "they_owe_me"),
    ("Bugun taksiga 25 ming sarfladim", "add_expense", None, "25000", None),
    ("Kecha 200 ming so'm maosh tushdi", "add_income", None, "200000", None),
])
def test_offline_parser(text, intent, name, amount, direction):
    r = parse_offline(text)
    assert (r.intent, r.person_name, r.amount, r.direction) == (intent, name, Decimal(amount), direction)
    assert r.confidence < 0.75  # always asks for confirmation


def test_offline_parser_rejects_unrelated():
    assert parse_offline("salom, qalaysan?") is None


@pytest.mark.parametrize("sent, received, minutes, linked", [
    ("1.003,00", "1.000,00", 0, True),        # owner's real example: 0.3% fee, small amount
    ("103.000,00", "100.000,00", 1, True),    # 3%
    ("6.000,00", "1.000,00", 0, True),        # fixed minimum fee on a tiny transfer
    ("110.000,00", "100.000,00", 0, False),   # 10% difference: not the same money
    ("100.500,00", "100.000,00", 9, False),   # too far apart in time
])
async def test_own_card_transfer_fee_tolerance(sf, settings, owner_id, sent, received, minutes, linked):
    out_msg = f"""🧾 Amaliyot
➖ {sent} UZS
📍 Octo Mobile P2P H2H>
💳 HUMOCARD *5592
🕓 17:38 09.11.2026
💰 12.345.021,85 UZS"""
    in_msg = f"""🎉 To'ldirish
➕ {received} UZS
📍 Octo Mobile P2P H2H>
💳 HUMOCARD *3798
🕓 17:{38 + minutes:02d} 09.11.2026
💰 1.796,22 UZS"""
    await run(sf, settings, owner_id, (in_msg, tg_date(d=9, h=12, mi=38 + minutes)), (out_msg, tg_date(d=9, h=12, mi=38)))
    types = {t.direction: t.transaction_type for t in await live_txs(sf)}
    if linked:
        assert types == {"in": TxType.TRANSFER, "out": TxType.TRANSFER}
    else:
        assert types["in"] == TxType.INCOME


@pytest.mark.parametrize("merchant, sub", [
    ("CLICK HUMO TO HUMO>T", "Sent to Others"),
    ("Humo to Visa>Tashken", "Sent to Others"),
    ("TBC KONVERSIYA>TASHK", "Own Cards"),
    ("TBC HUMO NA KOSHELEK", "Own Cards"),
])
async def test_card_to_card_names(sf, settings, owner_id, merchant, sub):
    msg = f"""💸 To'lov
➖ 50.000,00 UZS
📍 {merchant}
💳 HUMOCARD *5592
🕓 12:00 06.11.2026
💰 1.000.000,00 UZS"""
    _, ai, _ = await run(sf, settings, owner_id, (msg, tg_date(d=6, h=7)))
    [tx] = await live_txs(sf)
    assert tx.transaction_type == TxType.TRANSFER and ai.calls == []
    assert await cat_names(sf, tx) == ("Transfers", sub)
