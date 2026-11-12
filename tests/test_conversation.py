"""Several operations in one message, and replies that complete or correct an earlier message."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.assistant.memory import combine, find_reply, remember_reply
from app.assistant.service import Assistant
from app.models import Category, Debt, Transaction
from app.models.enums import TxSource
from tests.conftest import FakeAI

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
THREE = "sardorbek, avazbek, jonibek har biri 4000 so'mdan menda qarz bo'ldi, taxiga toladim"


def debt(name):
    return dict(intent="create_debt", person_name=name, amount="4000", currency="UZS", direction="they_owe_me",
                category="Transport", subcategory="Taxi", confidence=0.95)


def make(responder, sf, settings):
    ai = FakeAI(responder)
    return Assistant(sf, ai, settings), ai


async def test_each_person_gets_a_debt_and_one_undo_removes_all(sf, settings, owner_id):
    a, _ = make(lambda p, au: {"intent": "multiple", "confidence": 0.95,
                               "operations": [debt("Sardorbek"), debt("Avazbek"), debt("Jonibek")]}, sf, settings)
    r = await a.handle_text(THREE, source=TxSource.BOT_TEXT, idempotency_key="k1", tz=TZ, now=NOW)
    assert r.kind == "saved" and "3 operations" in r.text and r.undo == ("batch", "k1")
    async with sf() as s:
        debts = (await s.scalars(select(Debt).order_by(Debt.id))).all()
    assert [(d.original_amount, d.status) for d in debts] == [(4000, "open")] * 3
    # same message processed twice (retry): nothing duplicated
    await a.handle_text(THREE, source=TxSource.BOT_TEXT, idempotency_key="k1", tz=TZ, now=NOW)
    async with sf() as s:
        assert len((await s.scalars(select(Debt))).all()) == 3
    assert "Undone" in await a.undo("batch", "k1")
    async with sf() as s:
        assert {d.status for d in (await s.scalars(select(Debt))).all()} == {"cancelled"}


async def test_reply_with_missing_amount_completes_the_message(sf, settings, owner_id):
    def responder(prompt, audio):
        if "EARLIER" in prompt and "4000 sum" in prompt:
            return debt("Jonibek")
        return {**debt("Jonibek"), "amount": None, "clarification_question": "Qancha?"}

    a, ai = make(responder, sf, settings)
    first = await a.handle_text("jonibek menda qarz bo'ldi, taxiga toladim", source=TxSource.BOT_TEXT,
                                idempotency_key="k1", tz=TZ, now=NOW)
    assert first.kind == "clarify" and "Reply to this message" in first.text
    second = await a.handle_text("4000 sum", source=TxSource.BOT_TEXT, idempotency_key="k2", tz=TZ, now=NOW,
                                 context="jonibek menda qarz bo'ldi, taxiga toladim")
    assert second.kind == "saved"
    assert "jonibek menda qarz" in ai.calls[-1]  # the earlier message was sent along


async def test_reply_correction_replaces_the_wrong_record(sf, settings, owner_id):
    def responder(prompt, audio):
        if "groceriyga" in prompt:
            return dict(intent="add_expense", amount="4000", category="Shopping", subcategory="Groceries",
                        item="Qatiq", confidence=0.95)
        return dict(intent="add_expense", amount="4000", category="Food", subcategory="Snacks", item="Suzma",
                    confidence=0.95)

    a, _ = make(responder, sf, settings)
    first = await a.handle_text("4 ming so'mga suzma oldim", source=TxSource.BOT_TEXT, idempotency_key="k1", tz=TZ, now=NOW)
    assert first.kind == "saved"
    fixed = await a.handle_text("suzma bu groceriyga kiradi yani qatiq", source=TxSource.BOT_TEXT,
                                idempotency_key="k2", tz=TZ, now=NOW, context="4 ming so'mga suzma oldim",
                                replace=[first.undo])
    assert fixed.kind == "saved" and "Corrected" in fixed.text
    async with sf() as s:
        live = (await s.scalars(select(Transaction).where(Transaction.deleted_at.is_(None)))).all()
        assert len(live) == 1
        item = await s.get(Category, live[0].item_id)
        sub = await s.get(Category, live[0].subcategory_id)
    assert (sub.name, item.name) == ("Groceries", "Qatiq")


async def test_unrecognised_correction_keeps_the_old_record(sf, settings, owner_id):
    a, _ = make(lambda p, au: dict(intent="add_expense", amount="4000", category="Food", confidence=0.95)
                if "suzma oldim" in p and "EARLIER" not in p else dict(intent="unknown", confidence=0.9), sf, settings)
    first = await a.handle_text("4 ming so'mga suzma oldim", source=TxSource.BOT_TEXT, idempotency_key="k1", tz=TZ, now=NOW)
    r = await a.handle_text("rahmat", source=TxSource.BOT_TEXT, idempotency_key="k2", tz=TZ, now=NOW,
                            context="4 ming so'mga suzma oldim", replace=[first.undo])
    assert r.kind == "unknown"
    async with sf() as s:
        assert len((await s.scalars(select(Transaction).where(Transaction.deleted_at.is_(None)))).all()) == 1


async def test_reply_memory_keeps_what_the_owner_said(sf):
    await remember_reply(sf, 1, 10, user_text="jonibek qarz", kind="clarify")
    await remember_reply(sf, 1, 10, user_text=None, kind="saved", undo=("debt", "5"))  # confirm button edit
    r = await find_reply(sf, 1, 10)
    assert (r.user_text, r.kind, r.undo_kind, r.undo_ref) == ("jonibek qarz", "saved", "debt", "5")
    assert combine("a", "b") == "a\nb" and combine(None, "b") == "b"
