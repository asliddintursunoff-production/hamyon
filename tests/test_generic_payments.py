"""Payment gateways (MUNIS, QR, OPLATA…) don't reveal what was bought: never trust a guessed rule, always ask."""
from decimal import Decimal

from sqlalchemy import select

from app.bot.services.questions import load_tx, question_text
from app.models import Merchant, Transaction
from app.transactions.categories import resolve_full
from app.transactions.pipeline import MessageProcessor
from app.transactions.questions import load_for_question
from app.transactions.rules import looks_like_generic_payment
from tests.conftest import FakeAI, tg_date

MUNIS = """💸 To'lov
➖ 23.000,00 UZS
📍 CLICK MUNIS QR>TOSHK
💳 HUMOCARD *5592
🕓 10:50 06.11.2026
💰 11.989.155,85 UZS"""


def test_generic_names():
    for name in ("CLICK MUNIS QR>TOSHK", "CLICK OPLATA>TASHKEN", "AAB CLICK QR OPLATA", "QR online Ekvayring>",
                 "CLICK TEZ QR>Tashken", "CLICK PAYNET>TASHKEN", "Click Oplata po rek"):
        assert looks_like_generic_payment(name), name
    for name in ("CLICK P2P H2U ER>TAS", "UZMOBILE GSM click>T", "MCHJ NAVROZ ABDUAHAT", "OOO WB TAXI OPLATA>T", "SQRT CAFE", "AO CLICK>Toshkent"):
        assert not looks_like_generic_payment(name), name


async def _process(sf, settings, owner_id, ai, mid=1):
    proc = MessageProcessor(sf, ai, settings, owner_id)
    r = await proc.ingest(chat_id=1, message_id=mid, sender_id=1, text=MUNIS, date=tg_date(h=5, mi=50 + mid))
    await proc.process(r.row_id)
    async with sf() as s:
        return (await s.scalars(select(Transaction).order_by(Transaction.id.desc()))).first()


async def test_ai_guess_is_ignored_and_owner_is_asked(sf, settings, owner_id):
    async with sf.begin() as s:  # an old AI guess remembered for this merchant
        s.add(Merchant(normalized_name="CLICK MUNIS QR>TOSHK", direction="out", display_name="CLICK MUNIS QR>TOSHK",
                       transaction_type="expense", ai_confidence=Decimal("0.85")))
    ai = FakeAI(lambda p, a: {"transaction_type": "expense", "category": "Bills and Utilities"})
    tx = await _process(sf, settings, owner_id, ai)
    assert ai.calls == []                                   # gateway name: no AI guess
    assert (await load_for_question(sf, tx.id)) is not None  # always asked


async def test_owner_default_is_used_but_still_asked(sf, settings, owner_id):
    async with sf.begin() as s:
        cat, sub, item = await resolve_full(s, "Food", "Cafes", "INHA", "expense")
        s.add(Merchant(normalized_name="CLICK MUNIS QR>TOSHK", direction="out", display_name="CLICK MUNIS QR>TOSHK",
                       category_id=cat.id, subcategory_id=sub.id, item_id=item.id, transaction_type="expense",
                       is_spending=True, confirmed=True))
    tx = await _process(sf, settings, owner_id, FakeAI())
    async with sf() as s:
        t = await s.get(Transaction, tx.id)
        assert (t.category_id, t.subcategory_id, t.item_id, t.is_spending) == (cat.id, sub.id, item.id, True)
    assert (await load_for_question(sf, tx.id)) is not None
    text = question_text(await load_tx(sf, tx.id))
    assert "Food › Cafes › INHA?" in text
