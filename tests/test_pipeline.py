import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.ai.gemini import AIUnavailable
from app.models import BalanceSnapshot, Card, Category, Merchant, TelegramMessage, Transaction
from app.models.enums import ClassificationStatus, MsgStatus, TxType
from app.transactions.pipeline import MessageProcessor
from app.transactions.service import update_transaction_manual
from tests.conftest import HUMO_A, HUMO_B, HUMO_C, HUMO_D, FakeAI, tg_date

CHAT = 777


def classify_by_merchant(prompt, audio):
    prompt = prompt.split("<<<MESSAGE", 1)[1]  # look only at the bank message, not the category list
    if "WB TAXI" in prompt:
        return {"transaction_type": "expense", "amount": "16213.00", "currency": "UZS", "merchant": "OOO WB TAXI OPLATA>T",
                "card_last_four": "5592", "category": "Transport", "subcategory": "Taxi", "description": "Taxi payment",
                "is_spending": True, "confidence": 0.97, "needs_review": False}
    if "P2P" in prompt:
        return {"transaction_type": "transfer", "category": "Transfers", "subcategory": "P2P", "description": "P2P transfer",
                "is_spending": False, "confidence": 0.9, "needs_review": False}
    return {"transaction_type": "expense", "category": "Other", "subcategory": "Unclassified", "description": None,
            "is_spending": True, "confidence": 0.3, "needs_review": True, "ambiguity": "unknown merchant"}


async def ingest(proc, text, msg_id, date=None):
    return await proc.ingest(chat_id=CHAT, message_id=msg_id, sender_id=CHAT, text=text, date=date or tg_date())


async def get_tx(sf, msg_row_id):
    async with sf() as s:
        return (await s.scalars(select(Transaction).where(Transaction.original_message_id == msg_row_id))).one()


async def count(sf, model):
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def test_taxi_is_classified_and_balance_is_snapshot(sf, settings, owner_id):
    ai = FakeAI(classify_by_merchant)
    proc = MessageProcessor(sf, ai, settings, owner_id)
    r = await ingest(proc, HUMO_C, 3)
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.SAVED
    tx = await get_tx(sf, r.row_id)
    assert tx.transaction_type == TxType.EXPENSE and tx.is_spending
    assert tx.amount == Decimal("16213.00")
    async with sf() as s:
        cat = await s.get(Category, tx.category_id)
        sub = await s.get(Category, tx.subcategory_id)
        card = (await s.scalars(select(Card).where(Card.card_last_four == "5592"))).one()
        snaps = (await s.scalars(select(BalanceSnapshot))).all()
    assert (cat.name, sub.name) == ("Transport", "Taxi")
    assert tx.ai_confidence == Decimal("0.970") and not tx.needs_review
    # the reported balance is a snapshot, not a transaction
    assert card.current_balance == Decimal("13272858.60")
    assert len(snaps) == 1 and snaps[0].source == "transaction_message"
    assert await count(sf, Transaction) == 1


async def test_p2p_is_transfer_not_purchase(sf, settings, owner_id):
    proc = MessageProcessor(sf, FakeAI(classify_by_merchant), settings, owner_id)
    r = await ingest(proc, HUMO_B, 2)
    await proc.process(r.row_id)
    tx = await get_tx(sf, r.row_id)
    assert tx.transaction_type == TxType.TRANSFER
    assert tx.is_spending is False


async def test_outgoing_p2p_is_sent_to_others_without_ai(sf, settings, owner_id):
    ai = FakeAI(lambda p, a: AIUnavailable("down"))
    proc = MessageProcessor(sf, ai, settings, owner_id)
    r = await ingest(proc, HUMO_D, 4)
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.SAVED and ai.calls == []
    tx = await get_tx(sf, r.row_id)
    assert tx.transaction_type == TxType.TRANSFER and not tx.needs_review and not tx.is_spending
    async with sf() as s:
        assert (await s.get(Category, tx.category_id)).name == "Transfers"
        assert (await s.get(Category, tx.subcategory_id)).name == "Sent to Others"
    assert tx.amount == Decimal("100300.00")


async def test_unknown_merchant_is_not_guessed(sf, settings, owner_id):
    ai = FakeAI(classify_by_merchant)
    proc = MessageProcessor(sf, ai, settings, owner_id)
    r = await ingest(proc, HUMO_A, 1)
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.NEEDS_REVIEW
    tx = await get_tx(sf, r.row_id)
    async with sf() as s:
        assert (await s.get(Category, tx.category_id)).name == "Other"
    assert tx.needs_review
    # negative cache: the same unknown merchant again does not spend AI quota
    r2 = await ingest(proc, HUMO_A.replace("22:23", "23:00"), 10)
    await proc.process(r2.row_id)
    assert len(ai.calls) == 1


async def test_duplicate_message_never_creates_second_transaction(sf, settings, owner_id):
    ai = FakeAI(classify_by_merchant)
    proc = MessageProcessor(sf, ai, settings, owner_id)
    r1 = await ingest(proc, HUMO_C, 3)
    r2 = await ingest(proc, HUMO_C, 3)
    assert r1.created and not r2.created and r1.row_id == r2.row_id
    await proc.process(r1.row_id)
    assert await proc.process(r1.row_id) is None  # already saved -> not claimable
    assert await count(sf, Transaction) == 1
    assert await count(sf, TelegramMessage) == 1
    assert await count(sf, BalanceSnapshot) == 1


async def test_concurrent_processing_is_consistent(sf, settings, owner_id):
    ai = FakeAI(classify_by_merchant)
    proc = MessageProcessor(sf, ai, settings, owner_id)
    msgs = [(HUMO_A, 1, tg_date(d=5, h=17, mi=23)), (HUMO_B, 2, tg_date(d=5, h=17, mi=24)),
            (HUMO_C, 3, tg_date(d=6, h=4, mi=59)), (HUMO_D, 4, tg_date(d=6, h=7, mi=12))]
    rows = await asyncio.gather(*(ingest(proc, t, i, d) for t, i, d in msgs))
    # process every message twice, all at the same time, in reverse order
    await asyncio.gather(*(proc.process(r.row_id) for r in reversed(rows * 2)))
    assert await count(sf, Transaction) == 4
    assert await count(sf, BalanceSnapshot) == 4
    async with sf() as s:
        card = (await s.scalars(select(Card).where(Card.card_last_four == "5592"))).one()
        statuses = set((await s.scalars(select(TelegramMessage.processing_status))).all())
    # the newest message's balance wins regardless of processing order
    assert card.current_balance == Decimal("13170158.60")
    assert statuses <= {MsgStatus.SAVED, MsgStatus.NEEDS_REVIEW}


async def test_out_of_order_balance_does_not_overwrite_newer(sf, settings, owner_id):
    proc = MessageProcessor(sf, FakeAI(classify_by_merchant), settings, owner_id)
    newer = await ingest(proc, HUMO_D, 4, tg_date(d=6, h=7))
    older = await ingest(proc, HUMO_C, 3, tg_date(d=6, h=4))
    await proc.process(newer.row_id)
    await proc.process(older.row_id)
    async with sf() as s:
        card = (await s.scalars(select(Card).where(Card.card_last_four == "5592"))).one()
    assert card.current_balance == Decimal("13170158.60")


async def test_malformed_ai_json_is_retried_without_duplicates(sf, settings, owner_id):
    responses = ["{not json", None]

    def responder(prompt, audio):
        r = responses.pop(0)
        return r if r is not None else classify_by_merchant(prompt, audio)

    proc = MessageProcessor(sf, FakeAI(responder), settings, owner_id)
    r = await ingest(proc, HUMO_C, 3)
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.AWAITING_AI
    async with sf() as s:
        msg = await s.get(TelegramMessage, r.row_id)
        assert msg.attempts == 1 and msg.next_attempt_at is not None
        msg_next = msg.next_attempt_at
    assert await proc.process(r.row_id) is None  # backoff respected
    # simulate time passing
    async with sf.begin() as s:
        (await s.get(TelegramMessage, r.row_id)).next_attempt_at = msg_next - timedelta(hours=2)
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.SAVED
    assert await count(sf, Transaction) == 1
    tx = await get_tx(sf, r.row_id)
    assert tx.classification_status == ClassificationStatus.AI


async def test_ai_failures_end_in_needs_review(sf, settings, owner_id):
    settings.ai_max_message_attempts = 2
    proc = MessageProcessor(sf, FakeAI(lambda p, a: AIUnavailable("429")), settings, owner_id)
    r = await ingest(proc, HUMO_C, 3)
    await proc.process(r.row_id)
    async with sf.begin() as s:
        (await s.get(TelegramMessage, r.row_id)).next_attempt_at = None
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.NEEDS_REVIEW
    tx = await get_tx(sf, r.row_id)
    assert tx.needs_review and tx.classification_status == ClassificationStatus.FALLBACK


async def test_ai_amount_mismatch_forces_review_and_keeps_parsed_amount(sf, settings, owner_id):
    def lying(prompt, audio):
        d = classify_by_merchant(prompt, audio)
        d["amount"] = "999999.00"
        return d

    proc = MessageProcessor(sf, FakeAI(lying), settings, owner_id)
    r = await ingest(proc, HUMO_C, 3)
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.NEEDS_REVIEW
    tx = await get_tx(sf, r.row_id)
    assert tx.amount == Decimal("16213.00")


async def test_merchant_memory_avoids_ai_calls(sf, settings, owner_id):
    ai = FakeAI(classify_by_merchant)
    proc = MessageProcessor(sf, ai, settings, owner_id)
    r1 = await ingest(proc, HUMO_C, 3)
    await proc.process(r1.row_id)
    r2 = await ingest(proc, HUMO_C.replace("09:59", "18:30"), 30)
    out = await proc.process(r2.row_id)
    assert out.status == MsgStatus.SAVED
    assert len(ai.calls) == 1
    tx2 = await get_tx(sf, r2.row_id)
    assert tx2.classification_status == ClassificationStatus.RULE
    async with sf() as s:
        assert (await s.get(Category, tx2.subcategory_id)).name == "Taxi"


async def test_owner_correction_becomes_rule_and_is_never_overwritten(sf, settings, owner_id):
    ai = FakeAI(classify_by_merchant)
    proc = MessageProcessor(sf, ai, settings, owner_id)
    r1 = await ingest(proc, HUMO_A, 1)
    await proc.process(r1.row_id)
    tx = await get_tx(sf, r1.row_id)
    async with sf.begin() as s:
        await update_transaction_manual(s, tx.id, actor="test", category="Food", subcategory="Cafes", apply_to_merchant=True)
    # AI re-run must not touch an owner-edited transaction
    assert await proc.reclassify(tx.id) is False
    r2 = await ingest(proc, HUMO_A.replace("22:23", "08:00"), 11)
    await proc.process(r2.row_id)
    assert len(ai.calls) == 1  # rule used, no AI
    tx2 = await get_tx(sf, r2.row_id)
    async with sf() as s:
        assert (await s.get(Category, tx2.category_id)).name == "Food"
        assert (await s.get(Category, tx2.subcategory_id)).name == "Cafes"
        m = (await s.scalars(select(Merchant).where(Merchant.normalized_name == "MCHJ NAVROZ ABDUAHAT"))).one()
        assert m.confirmed
        tx1 = await s.get(Transaction, tx.id)
        assert tx1.user_edited and tx1.classification_status == ClassificationStatus.MANUAL


async def test_stale_lease_recovery(sf, settings, owner_id):
    proc = MessageProcessor(sf, FakeAI(classify_by_merchant), settings, owner_id)
    r = await ingest(proc, HUMO_C, 3)
    claimed = await proc._claim(r.row_id)  # a worker claimed it and then "crashed"
    assert claimed is not None
    assert await proc.process(r.row_id) is None  # lease still held
    await proc.reset_stale_leases()  # application restart
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.SAVED
    assert await count(sf, Transaction) == 1


@pytest.mark.parametrize("kind_text", ["Reklama: yangi aksiya!"])
async def test_non_financial_messages_are_ignored(sf, settings, owner_id, kind_text):
    proc = MessageProcessor(sf, FakeAI(classify_by_merchant), settings, owner_id)
    r = await ingest(proc, kind_text, 99)
    out = await proc.process(r.row_id)
    assert out.status == MsgStatus.IGNORED
    assert await count(sf, Transaction) == 0
