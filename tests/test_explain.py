"""'What was this money for?' — splitting a bank payment into parts, debts included."""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.ai.schemas import SplitLine
from app.analytics import service as an
from app.assistant.service import Assistant
from app.debts.service import create_debt, debt_overview
from app.models import Category, Transaction, TransactionQuestion
from app.models.enums import DebtDirection, TxSource, TxType
from app.transactions.pipeline import MessageProcessor
from app.transactions.questions import load_for_question, remember_question
from app.transactions.splits import SplitError, apply_splits, plan_lines, undo_explanation
from app.utils.timeutil import utcnow
from tests.conftest import FakeAI, tg_date

TZ = ZoneInfo("Asia/Tashkent")
D = Decimal

SENT = """💸 Amaliyot
➖ 49.000,00 UZS
📍 Octo Mobile P2P H2U>
💳 HUMOCARD *5592
🕓 12:40 06.11.2026
💰 1.000.000,00 UZS"""


async def make_tx(sf, settings, owner_id, text=SENT):
    proc = MessageProcessor(sf, FakeAI(), settings, owner_id)
    r = await proc.ingest(chat_id=1, message_id=1, sender_id=1, text=text, date=tg_date(d=6, h=7, mi=40))
    await proc.process(r.row_id)
    async with sf() as s:
        return (await s.scalars(select(Transaction))).one()


def L(kind, amount=None, category=None, subcategory=None, person=None):
    return SplitLine(kind=kind, amount=amount, category=category, subcategory=subcategory, person_name=person)


# ---------------------------------------------------------------- planning
def test_single_purpose_takes_whole_amount():
    lines, problems = plan_lines([L("expense", None, "Food")], D("49000"), "out")
    assert [ln.amount for ln in lines] == [D("49000")] and problems == []


def test_rest_is_filled_in():
    lines, problems = plan_lines([L("expense", "20000", "Food"), L("expense", None, "Transport", "Taxi")], D("49000"), "out")
    assert [ln.amount for ln in lines] == [D("20000"), D("29000")] and problems == []


def test_parts_more_than_payment_rejected():
    with pytest.raises(SplitError):
        plan_lines([L("expense", "30000"), L("expense", "30000")], D("49000"), "out")


def test_unexplained_rest_needs_confirmation():
    lines, problems = plan_lines([L("expense", "20000", "Food"), L("expense", "10000", "Transport")], D("49000"), "out")
    assert lines[-1].amount == D("19000") and lines[-1].category == "Other" and problems


def test_kind_must_fit_direction_and_debts_need_a_person():
    with pytest.raises(SplitError):
        plan_lines([L("income")], D("49000"), "out")
    with pytest.raises(SplitError):
        plan_lines([L("debt_repay", "20000")], D("49000"), "out")


# ---------------------------------------------------------------- applying
async def totals(sf, owner_id):
    f = an.Filters(datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 12, 1, tzinfo=TZ), "UZS")
    async with sf() as s:
        summ = await an.summary(s, f)
        cats = {c["name"]: c["total"] for c in await an.by_category(s, f)}
        subs = {(x["category"], x["subcategory"]): x["total"] for x in await an.by_subcategory(s, f)}
    return summ, cats, subs


async def owe(sf, name):
    async with sf() as s:
        people = {p.name: p for p in (await debt_overview(s, include_settled=True)).people}
    return people[name].i_owe_them if name in people else D("0")


async def test_transfer_to_others_is_not_spending_until_explained(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    summ, _, _ = await totals(sf, owner_id)
    assert summ["expenses"] == D("0") and summ["transfers_out"] == D("49000")
    lines, _ = plan_lines([L("expense", "20000", "Food"), L("expense", None, "Transport", "Taxi")], tx.amount, "out")
    async with sf.begin() as s:
        await apply_splits(s, tx.id, lines, actor="test", key="k1", source=TxSource.BOT_TEXT)
    summ, cats, subs = await totals(sf, owner_id)
    assert summ["expenses"] == D("49000") and summ["transfers_out"] == D("0")
    assert cats == {"Food": D("20000"), "Transport": D("29000")}
    assert subs[("Transport", "Taxi")] == D("29000")


async def test_debt_repayment_part_updates_debt_ledger(sf, settings, owner_id):
    async with sf.begin() as s:
        await create_debt(s, person_name="Orzubek", direction=DebtDirection.I_OWE_THEM, amount=D("30000"),
                          currency="UZS", description=None, occurred_at=utcnow(), source="bot_text", idempotency_key=None)
    tx = await make_tx(sf, settings, owner_id)
    lines, _ = plan_lines([L("debt_repay", "20000", person="Orzubekka"), L("expense", None, "Food")], tx.amount, "out")
    async with sf.begin() as s:
        await apply_splits(s, tx.id, lines, actor="test", key="k2", source=TxSource.BOT_TEXT)
    assert await owe(sf, "Orzubek") == D("10000")
    summ, cats, _ = await totals(sf, owner_id)
    assert summ["expenses"] == D("29000") and cats == {"Food": D("29000")}  # the debt part is not spending

    # a new answer replaces the old one, including its debt effect
    lines2, _ = plan_lines([L("expense", None, "Shopping")], tx.amount, "out")
    async with sf.begin() as s:
        await apply_splits(s, tx.id, lines2, actor="test", key="k3", source=TxSource.BOT_TEXT)
    assert await owe(sf, "Orzubek") == D("30000")
    _, cats, _ = await totals(sf, owner_id)
    assert cats == {"Shopping": D("49000")}

    # undo restores the original "Sent to Others" transfer
    async with sf.begin() as s:
        await undo_explanation(s, tx.id, actor="test")
    summ, cats, _ = await totals(sf, owner_id)
    assert cats == {} and summ["transfers_out"] == D("49000")
    async with sf() as s:
        t = await s.get(Transaction, tx.id)
        assert not t.has_splits and t.transaction_type == TxType.TRANSFER
        assert (await s.get(Category, t.subcategory_id)).name == "Sent to Others"


async def test_lending_part_creates_debt(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    lines, _ = plan_lines([L("debt_new", None, person="Ali")], tx.amount, "out")
    async with sf.begin() as s:
        await apply_splits(s, tx.id, lines, actor="test", key="k4", source=TxSource.BOT_TEXT)
    async with sf() as s:
        ali = {p.name: p for p in (await debt_overview(s)).people}["Ali"]
    assert ali.they_owe_me == D("49000")


# ---------------------------------------------------------------- assistant flow
def split_ai(lines, transcript=None, confidence=0.95):
    return FakeAI(lambda prompt, audio: {"transcript": transcript, "lines": lines, "confidence": confidence})


async def test_text_answer_is_applied(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    a = Assistant(sf, split_ai([{"kind": "expense", "amount": "20000", "category": "Food"},
                                {"kind": "expense", "amount": None, "category": "Transport", "subcategory": "Taxi"}]), settings)
    _, reply = await a.explain_transaction(tx.id, text="20 ming ovqat, qolgani taksi", source=TxSource.BOT_TEXT,
                                           idempotency_key="bot:1:9", tz=TZ)
    assert reply.kind == "saved" and reply.undo == ("explain", str(tx.id))
    _, cats, _ = await totals(sf, owner_id)
    assert cats == {"Food": D("20000"), "Transport": D("29000")}


async def test_voice_answer_uses_transcript_and_amount_check(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    # AI claims 25 000 but the transcript says 20 ming -> must ask for confirmation, nothing saved yet
    a = Assistant(sf, split_ai([{"kind": "expense", "amount": "25000", "category": "Food"},
                                {"kind": "expense", "amount": None, "category": "Transport"}],
                               transcript="20 ming ovqat qolgani taksi"), settings)
    heard, reply = await a.explain_transaction(tx.id, audio=(b"ogg", "audio/ogg"), source=TxSource.BOT_VOICE,
                                               idempotency_key="v1", tz=TZ)
    assert heard == "20 ming ovqat qolgani taksi" and reply.kind == "confirm"
    async with sf() as s:
        assert not (await s.get(Transaction, tx.id)).has_splits
    done = await a.confirm(reply.pending_id, tz=TZ)
    assert done.kind == "saved"


async def test_paying_more_than_owed_asks_and_records_the_extra(sf, settings, owner_id):
    async with sf.begin() as s:
        await create_debt(s, person_name="Orzubek", direction=DebtDirection.I_OWE_THEM, amount=D("10000"),
                          currency="UZS", description=None, occurred_at=utcnow(), source="bot_text", idempotency_key=None)
    tx = await make_tx(sf, settings, owner_id)  # −49 000
    a = Assistant(sf, split_ai([{"kind": "debt_repay", "amount": None, "person_name": "Orzubek"}]), settings)
    _, reply = await a.explain_transaction(tx.id, text="Orzubekka qarzimni qaytardim", source=TxSource.BOT_TEXT,
                                           idempotency_key="x", tz=TZ)
    assert reply.kind == "confirm" and "Orzubek owing you" in reply.text
    assert await owe(sf, "Orzubek") == D("10000")  # nothing changed before confirming
    done = await a.confirm(reply.pending_id, tz=TZ)
    assert done.kind == "saved"
    async with sf() as s:
        p = {x.name: x for x in (await debt_overview(s, include_settled=True)).people}["Orzubek"]
    assert p.i_owe_them == D("0") and p.they_owe_me == D("39000")


async def test_quick_button_answer(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    a = Assistant(sf, FakeAI(), settings)
    reply = await a.quick_explain(tx.id, "taxi")
    assert reply.kind == "saved"
    _, cats, subs = await totals(sf, owner_id)
    assert subs[("Transport", "Taxi")] == D("49000")


# ---------------------------------------------------------------- when to ask
async def test_question_only_for_unclear_payments(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    assert (await load_for_question(sf, tx.id)) is not None          # sent to someone's card -> ask
    await remember_question(sf, tx.id, chat_id=5, message_id=77)
    assert (await load_for_question(sf, tx.id)) is None              # never asked twice
    async with sf() as s:
        assert (await s.scalars(select(TransactionQuestion))).one().message_id == 77


async def test_no_question_for_clear_or_explained_payments(sf, settings, owner_id):
    taxi = SENT.replace("Octo Mobile P2P H2U>", "OOO WB TAXI OPLATA>T")
    tx = await make_tx(sf, settings, owner_id, taxi)
    async with sf.begin() as s:
        t = await s.get(Transaction, tx.id)
        t.category_id = (await s.scalars(select(Category).where(Category.name == "Transport"))).first().id
    assert (await load_for_question(sf, tx.id)) is None



class RecordingNotifier:
    def __init__(self):
        self.calls = []

    async def transaction_processed(self, tx_id):
        self.calls.append(tx_id)


async def test_rule_classified_transfer_triggers_question(sf, settings, owner_id):
    """Transfers are classified without AI; the owner must still be asked about them."""
    notifier = RecordingNotifier()
    proc = MessageProcessor(sf, FakeAI(), settings, owner_id, notifier=notifier)
    r = await proc.ingest(chat_id=1, message_id=1, sender_id=1, text=SENT, date=utcnow())
    await proc.process(r.row_id)
    async with sf() as s:
        tx = (await s.scalars(select(Transaction))).one()
    assert notifier.calls == [tx.id]
    assert (await load_for_question(sf, tx.id)) is not None


async def test_missed_questions_are_sent_after_restart(sf, settings, owner_id):
    proc = MessageProcessor(sf, FakeAI(), settings, owner_id)  # no notifier: e.g. bot was down
    r = await proc.ingest(chat_id=1, message_id=1, sender_id=1, text=SENT, date=utcnow())
    await proc.process(r.row_id)
    notifier = RecordingNotifier()
    proc.notifier = notifier
    await proc.notify_recent()
    await proc.notify_recent()  # only once
    assert len(notifier.calls) == 1


# ---------------------------------------------------------------- item level (category › sub › item)
async def test_item_level_is_stored_and_reported(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    a = Assistant(sf, split_ai([{"kind": "expense", "amount": "20000", "category": "Food", "subcategory": "Fast Food", "item": "Hot dog"},
                                {"kind": "expense", "amount": None, "category": "Shopping", "subcategory": "Clothing", "item": "kurtka"}]), settings)
    _, reply = await a.explain_transaction(tx.id, text="20 ming hot dog, qolgani kurtka", source=TxSource.BOT_TEXT,
                                           idempotency_key="i1", tz=TZ)
    assert reply.kind == "saved" and "Food › Fast Food › Hot dog" in reply.text
    f = an.Filters(datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 12, 1, tzinfo=TZ), "UZS")
    async with sf() as s:
        data = await an.app_overview(s, f)
    cats = {c["name"]: c for c in data["categories"]}
    food_sub = cats["Food"]["subs"][0]
    assert food_sub["subcategory"] == "Fast Food" and food_sub["items"][0] == {"item": "Hot dog", "total": D("20000"), "count": 1}
    assert cats["Shopping"]["subs"][0]["items"][0]["item"] == "Kurtka"   # capitalised, stored under Clothing


async def test_item_spellings_merge_and_item_without_sub_becomes_sub(sf, owner_id):
    from app.transactions.categories import resolve_full

    async with sf.begin() as s:
        a = await resolve_full(s, "Food", "Fast Food", "Hot dog", "expense")
        b = await resolve_full(s, "food", "fast food", "hotdog", "expense")
        c = await resolve_full(s, "Food", None, "Lavash", "expense")
    assert a[2].id == b[2].id and a[2].parent_id == a[1].id
    assert c[1].name == "Lavash" and c[2] is None


async def test_question_texts(sf, settings, owner_id):
    from app.bot.services.questions import done_text, load_tx, question_markup, question_text

    tx = await make_tx(sf, settings, owner_id)
    t = await load_tx(sf, tx.id)
    assert question_text(t).startswith("❓ <b>−49 000 UZS")
    labels = [b.text for row in question_markup(tx.id, False).inline_keyboard for b in row]
    assert labels == ["🍔 Food", "🛒 Groceries", "🚕 Taxi", "🛍 Shopping", "✖️"]  # no "transfer", no "bills"
    await Assistant(sf, FakeAI(), settings).quick_explain(tx.id, "taxi")
    t = await load_tx(sf, tx.id)
    assert done_text(t) == "✅ <b>−49 000 UZS · Octo Mobile P2P H2U&gt;</b>\nTransport › Taxi"


# ---------------------------------------------------------------- estimated prices
def test_estimates_are_scaled_to_the_payment():
    lines, problems = plan_lines([
        SplitLine(kind="expense", amount="9000", estimated=True, category="Food", subcategory="Snacks", item="Coca-Cola"),
        SplitLine(kind="expense", amount="9000", estimated=True, category="Food", subcategory="Snacks", item="Snickers"),
    ], D("15000"), "out")
    assert [ln.amount for ln in lines] == [D("7500"), D("7500")] and problems == []
    assert all(ln.estimated and not ln.stated for ln in lines)


def test_stated_prices_are_kept_and_estimates_fill_the_rest():
    lines, _ = plan_lines([
        SplitLine(kind="expense", amount="10000", category="Food", item="Lavash"),
        SplitLine(kind="expense", amount="3000", estimated=True, category="Food", subcategory="Drinks", item="Cola"),
        SplitLine(kind="expense", amount="3000", estimated=True, category="Food", subcategory="Snacks", item="Chips"),
    ], D("20000"), "out")
    assert [ln.amount for ln in lines] == [D("10000"), D("5000"), D("5000")]


async def test_estimated_parts_saved_without_confirmation(sf, settings, owner_id):
    tx = await make_tx(sf, settings, owner_id)
    a = Assistant(sf, split_ai([
        {"kind": "expense", "amount": "26000", "estimated": True, "category": "Food", "subcategory": "Snacks", "item": "Coca-Cola"},
        {"kind": "expense", "amount": "23000", "estimated": True, "category": "Food", "subcategory": "Snacks", "item": "Snickers"},
    ], transcript="Coca Cola bilan snickers edim"), settings)
    heard, reply = await a.explain_transaction(tx.id, audio=(b"ogg", "audio/ogg"), source=TxSource.BOT_VOICE,
                                               idempotency_key="e1", tz=TZ)
    assert reply.kind == "saved" and "≈" in reply.text
    from app.bot.services.questions import done_text, load_tx
    text = done_text(await load_tx(sf, tx.id))
    assert "Food › Snacks › Coca-Cola — ≈26 000" in text and "Food › Snacks › Snickers — ≈23 000" in text


async def test_repaying_a_purchase_counts_as_spending_and_reduces_debt(sf, settings, owner_id):
    async with sf.begin() as s:
        await create_debt(s, person_name="Orzubek", direction=DebtDirection.I_OWE_THEM, amount=D("60000"),
                          currency="UZS", description=None, occurred_at=utcnow(), source="bot_text", idempotency_key=None)
    tx = await make_tx(sf, settings, owner_id)  # −49 000
    a = Assistant(sf, split_ai([{"kind": "debt_repay", "amount": None, "person_name": "Orzubek",
                                 "category": "Food", "subcategory": "Snacks", "item": "Suv"}]), settings)
    _, reply = await a.explain_transaction(tx.id, text="Orzubek suv olib bergan edi, shuni to'ladim",
                                           source=TxSource.BOT_TEXT, idempotency_key="w1", tz=TZ)
    assert reply.kind == "saved" and "Food › Snacks › Suv" in reply.text
    assert await owe(sf, "Orzubek") == D("11000")              # debt went down
    summ, cats, subs = await totals(sf, owner_id)
    assert summ["expenses"] == D("49000") and subs[("Food", "Snacks")] == D("49000")   # and it is spending
    from app.bot.services.questions import done_text, load_tx
    assert "Orzubek — debt repaid · Food › Snacks › Suv" in done_text(await load_tx(sf, tx.id))


async def test_repaying_borrowed_cash_is_not_spending(sf, settings, owner_id):
    async with sf.begin() as s:
        await create_debt(s, person_name="Orzubek", direction=DebtDirection.I_OWE_THEM, amount=D("60000"),
                          currency="UZS", description=None, occurred_at=utcnow(), source="bot_text", idempotency_key=None)
    tx = await make_tx(sf, settings, owner_id)
    lines, _ = plan_lines([L("debt_repay", None, person="Orzubek")], tx.amount, "out")
    async with sf.begin() as s:
        await apply_splits(s, tx.id, lines, actor="test", key="c1", source=TxSource.BOT_TEXT)
    summ, _, _ = await totals(sf, owner_id)
    assert summ["expenses"] == D("0") and await owe(sf, "Orzubek") == D("11000")



# ---------------------------------------------------------------- debts with a purpose (owner's Coca-Cola example)
async def say(a, text, key, intent):
    a.ai = FakeAI(lambda p, au: intent)
    return await a.handle_text(text, source=TxSource.BOT_TEXT, idempotency_key=key, tz=TZ, now=utcnow())


async def test_debt_for_cola_becomes_spending_only_when_repaid(sf, settings, owner_id):
    a = Assistant(sf, FakeAI(), settings)
    # 1) "Orzubekdan qarz bo'ldim, bugun menga coca cola olib bergan edi 8000 so'm" -> debt with purpose, no spending yet
    r = await say(a, "Orzubekdan qarz bo'ldim, menga coca cola olib bergan edi 8000 so'm", "d1", {
        "intent": "create_debt", "person_name": "Orzubek", "amount": "8000", "direction": "i_owe_them",
        "category": "Food", "subcategory": "Snacks", "item": "Coca-Cola", "confidence": 0.95})
    assert r.kind == "saved" and "Coca-Cola" in r.text
    summ, _, _ = await totals(sf, owner_id)
    assert summ["expenses"] == D("0") and await owe(sf, "Orzubek") == D("8000")

    # 2) "Orzubekka qarzni to'ladim, 5000" -> 5000 Coca-Cola spending, 3000 still owed
    r = await say(a, "Orzubekka 5000 qarzimni to'ladim", "p1", {
        "intent": "repay_debt", "person_name": "Orzubek", "amount": "5000", "direction": "i_owe_them", "confidence": 0.95})
    assert r.kind == "saved"
    _, _, subs = await totals(sf, owner_id)
    assert subs[("Food", "Snacks")] == D("5000") and await owe(sf, "Orzubek") == D("3000")

    # 3) another debt without purpose, then "6000 to'ladim": 3000 finishes Coca-Cola, 3000 pays the cash loan
    async with sf.begin() as s:
        await create_debt(s, person_name="Orzubek", direction=DebtDirection.I_OWE_THEM, amount=D("10000"),
                          currency="UZS", description="naqd", occurred_at=utcnow(), source="bot_text", idempotency_key=None)
    r = await say(a, "Orzubekka 6000 to'ladim", "p2", {
        "intent": "repay_debt", "person_name": "Orzubek", "amount": "6000", "direction": "i_owe_them", "confidence": 0.95})
    _, _, subs = await totals(sf, owner_id)
    assert subs[("Food", "Snacks")] == D("8000")          # Coca-Cola fully counted now
    assert await owe(sf, "Orzubek") == D("7000")           # 10 000 cash loan - 3 000

    # undo of the last repayment removes its spending and restores the debt
    assert "Undone" in await a.undo("repayment", "p2")
    _, _, subs = await totals(sf, owner_id)
    assert subs[("Food", "Snacks")] == D("5000") and await owe(sf, "Orzubek") == D("13000")


async def test_overpayment_in_chat_needs_confirmation(sf, settings, owner_id):
    a = Assistant(sf, FakeAI(), settings)
    await say(a, "Alidan 3000 qarz", "d", {"intent": "create_debt", "person_name": "Ali", "amount": "3000",
                                          "direction": "i_owe_them", "confidence": 0.95})
    r = await say(a, "Aliga 6000 qaytardim", "p", {"intent": "repay_debt", "person_name": "Ali", "amount": "6000",
                                                   "direction": "i_owe_them", "confidence": 0.95})
    assert r.kind == "confirm" and "will owe you" in r.text
    done = await a.confirm(r.pending_id, tz=TZ)
    assert done.kind == "saved"
    async with sf() as s:
        p = {x.name: x for x in (await debt_overview(s, include_settled=True)).people}["Ali"]
    assert p.i_owe_them == D("0") and p.they_owe_me == D("3000")


async def test_bank_transfer_repayment_uses_debt_purposes(sf, settings, owner_id):
    a = Assistant(sf, FakeAI(), settings)
    await say(a, "Orzubek menga 20000 lik lavash olib berdi", "d", {"intent": "create_debt", "person_name": "Orzubek", "amount": "20000", "direction": "i_owe_them",
                            "category": "Food", "subcategory": "Fast Food", "item": "Lavash", "confidence": 0.95})
    async with sf.begin() as s:
        await create_debt(s, person_name="Orzubek", direction=DebtDirection.I_OWE_THEM, amount=D("50000"),
                          currency="UZS", description="naqd", occurred_at=utcnow(), source="bot_text", idempotency_key=None)
    tx = await make_tx(sf, settings, owner_id)  # −49 000 transfer answered "qarzimni to'ladim"
    lines, _ = plan_lines([L("debt_repay", None, person="Orzubek")], tx.amount, "out")
    async with sf.begin() as s:
        await apply_splits(s, tx.id, lines, actor="test", key="b1", source=TxSource.BOT_TEXT)
    summ, _, subs = await totals(sf, owner_id)
    assert subs[("Food", "Fast Food")] == D("20000") and summ["expenses"] == D("20000")  # lavash part is spending
    assert await owe(sf, "Orzubek") == D("21000")                                       # 70 000 - 49 000
    from app.bot.services.questions import done_text, load_tx
    text = done_text(await load_tx(sf, tx.id))
    assert "Food › Fast Food › Lavash — 20 000" in text and "debt repaid — 29 000" in text
