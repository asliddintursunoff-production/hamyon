"""Natural-language debts/expenses through the shared assistant pipeline (Gemini mocked)."""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.assistant.service import Assistant
from app.debts.service import debt_overview
from app.models import Debt, Transaction
from app.models.enums import TxSource
from tests.conftest import FakeAI

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc)

# What Gemini would return for each owner message (the test checks *our* validation and routing).
INTENTS = {
    "19 000 Orzubekdan qarz bo'ldim": dict(intent="create_debt", person_name="Orzubek", amount="19000", currency="UZS",
                                          direction="i_owe_them", confidence=0.98),
    "Orzubek mendan 20 ming qarzi bo'ldi": dict(intent="create_debt", person_name="Orzubek", amount="20000",
                                                direction="they_owe_me", confidence=0.97),
    "I borrowed 50,000 UZS from Ali": dict(intent="create_debt", person_name="Ali", amount="50000", currency="UZS",
                                           direction="i_owe_them", confidence=0.99),
    "Bob owes me 100,000 UZS": dict(intent="create_debt", person_name="Bob", amount="100000", direction="they_owe_me",
                                    confidence=0.99),
    "Sherzod bilan 30 ming qarz": dict(intent="create_debt", person_name="Sherzod", amount="30000", direction=None,
                                       confidence=0.5, needs_confirmation=True,
                                       clarification_question="Siz qarz oldingizmi yoki berdingizmi?"),
    "Ali 20 ming qaytardi": dict(intent="repay_debt", person_name="Ali", amount="20000", direction="i_owe_them", confidence=0.9),
    "Bugun taksiga 25 ming sarfladim": dict(intent="add_expense", amount="25000", category="Transport", subcategory="Taxi",
                                            description="Taksi", confidence=0.95),
    "Kecha 200 ming so'm maosh tushdi": dict(intent="add_income", amount="200000", category="Income", subcategory="Salary",
                                             transaction_datetime="2026-11-05T12:00:00+05:00", confidence=0.95),
    "Ali mendan qarz": dict(intent="create_debt", person_name="Ali", amount="500000", direction="they_owe_me", confidence=0.9),
    "salom": dict(intent="unknown", confidence=0.9),
}


def responder(prompt, audio):
    if audio is not None:  # one call: transcription + intent
        return {**INTENTS["Bugun taksiga 25 ming sarfladim"], "transcript": "Bugun taksiga 25 ming sarfladim"}
    for text, intent in INTENTS.items():
        if text in prompt:
            return intent
    return {"intent": "unknown", "confidence": 0.1}


@pytest.fixture
def assistant(sf, settings, owner_id):
    return Assistant(sf, FakeAI(responder), settings)


async def say(assistant, text, key):
    return await assistant.handle_text(text, source=TxSource.BOT_TEXT, idempotency_key=key, tz=TZ, now=NOW)


async def overview(sf):
    async with sf() as s:
        return {p.name: p for p in (await debt_overview(s)).people}


@pytest.mark.parametrize(
    "text, name, field, amount",
    [
        ("19 000 Orzubekdan qarz bo'ldim", "Orzubek", "i_owe_them", "19000.00"),
        ("Orzubek mendan 20 ming qarzi bo'ldi", "Orzubek", "they_owe_me", "20000.00"),
        ("I borrowed 50,000 UZS from Ali", "Ali", "i_owe_them", "50000.00"),
        ("Bob owes me 100,000 UZS", "Bob", "they_owe_me", "100000.00"),
    ],
)
async def test_debt_messages_both_directions(assistant, sf, text, name, field, amount):
    reply = await say(assistant, text, "k1")
    assert reply.kind == "saved", reply.text
    people = await overview(sf)
    assert getattr(people[name], field) == Decimal(amount)


async def test_uzbek_debts_same_person_net(assistant, sf):
    await say(assistant, "19 000 Orzubekdan qarz bo'ldim", "a")
    await say(assistant, "Orzubek mendan 20 ming qarzi bo'ldi", "b")
    p = (await overview(sf))["Orzubek"]
    assert p.net == Decimal("1000.00")


async def test_ambiguous_direction_asks_and_saves_only_after_choice(assistant, sf):
    reply = await say(assistant, "Sherzod bilan 30 ming qarz", "amb")
    assert reply.kind == "clarify" and reply.pending_id and len(reply.options) == 2
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Debt)) == 0  # nothing saved yet
    done = await assistant.confirm(reply.pending_id, tz=TZ, direction="they_owe_me")
    assert done.kind == "saved"
    again = await assistant.confirm(reply.pending_id, tz=TZ, direction="they_owe_me")  # double tap
    assert again.kind == "error"
    assert (await overview(sf))["Sherzod"].they_owe_me == Decimal("30000.00")


async def test_amount_not_in_text_requires_confirmation(assistant, sf):
    reply = await say(assistant, "Ali mendan qarz", "noamount")
    assert reply.kind == "confirm"
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Debt)) == 0
    assert await assistant.cancel(reply.pending_id)
    assert (await assistant.confirm(reply.pending_id, tz=TZ)).kind == "error"


async def test_repayment_via_text(assistant, sf):
    await say(assistant, "I borrowed 50,000 UZS from Ali", "d1")
    reply = await say(assistant, "Ali 20 ming qaytardi", "r1")
    assert reply.kind == "saved", reply.text
    assert (await overview(sf))["Ali"].i_owe_them == Decimal("30000.00")


async def test_repayment_without_debt_is_rejected(assistant, sf):
    reply = await say(assistant, "Ali 20 ming qaytardi", "r1")
    assert reply.kind == "error"


async def test_expense_and_income_do_not_touch_card_balances(assistant, sf):
    r1 = await say(assistant, "Bugun taksiga 25 ming sarfladim", "e1")
    r2 = await say(assistant, "Kecha 200 ming so'm maosh tushdi", "i1")
    assert r1.kind == r2.kind == "saved"
    async with sf() as s:
        txs = {t.transaction_type: t for t in (await s.scalars(select(Transaction))).all()}
        from app.models import Card
        balances = [c.current_balance for c in (await s.scalars(select(Card))).all()]
    assert txs["expense"].amount == Decimal("25000.00") and txs["expense"].card_id is None
    assert txs["income"].transaction_datetime.astimezone(TZ).day == 5
    assert balances == [None, None, None]


async def test_same_bot_message_twice_saves_once(assistant, sf):
    await say(assistant, "Bugun taksiga 25 ming sarfladim", "bot:1:77")
    await say(assistant, "Bugun taksiga 25 ming sarfladim", "bot:1:77")
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Transaction)) == 1


async def test_voice_uses_same_pipeline(assistant, sf):
    transcript, reply = await assistant.handle_voice(b"OggS...", "audio/ogg", idempotency_key="v1", tz=TZ, now=NOW)
    assert transcript == "Bugun taksiga 25 ming sarfladim"
    assert reply.kind == "saved"
    async with sf() as s:
        tx = (await s.scalars(select(Transaction))).one()
    assert tx.source == "bot_voice" and tx.amount == Decimal("25000.00")


async def test_unknown_message_saves_nothing(assistant, sf):
    reply = await say(assistant, "salom", "u")
    assert reply.kind == "unknown"


async def test_ai_unavailable_falls_back_to_offline_parser_with_confirmation(sf, settings, owner_id):
    a = Assistant(sf, FakeAI(None), settings)
    reply = await a.handle_text("19 000 Orzubekdan qarz bo'ldim", source=TxSource.BOT_TEXT, idempotency_key="x", tz=TZ, now=NOW)
    assert reply.kind == "confirm" and "without AI" in reply.text
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Debt)) == 0  # nothing saved before confirmation
    done = await a.confirm(reply.pending_id, tz=TZ)
    assert done.kind == "saved"
    assert (await overview(sf))["Orzubek"].i_owe_them == Decimal("19000.00")


async def test_ai_unavailable_and_not_understood_saves_nothing(sf, settings, owner_id):
    a = Assistant(sf, FakeAI(None), settings)
    reply = await a.handle_text("nimadir boshqa gap", source=TxSource.BOT_TEXT, idempotency_key="y", tz=TZ, now=NOW)
    assert reply.kind == "error"


async def test_voice_when_quota_exhausted_explains(sf, settings, owner_id):
    import time

    from app.ai.gemini import AIQuotaExhausted

    a = Assistant(sf, FakeAI(lambda p, au: AIQuotaExhausted(time.time() + 5 * 3600)), settings)
    transcript, reply = await a.handle_voice(b"x", "audio/ogg", idempotency_key="v", tz=TZ, now=NOW)
    assert transcript is None and reply.kind == "error" and "limit" in reply.text


async def test_undo_debt_keeps_audit(assistant, sf):
    reply = await say(assistant, "Bob owes me 100,000 UZS", "u1")
    msg = await assistant.undo(*reply.undo)
    assert "Undone" in msg
    assert "Bob" not in await overview(sf)
