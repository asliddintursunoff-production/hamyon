"""Natural-language assistant shared by typed and voice messages.

Pipeline: text -> Gemini intent -> strict schema -> deterministic validation (amount cross-check,
direction, person, date) -> either save immediately or ask for confirmation/clarification.
Voice messages are transcribed first and then go through exactly the same function.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.ai.gemini import AIClient, AIInvalidResponse, AIQuotaExhausted, AIUnavailable
from app.assistant.offline import parse_offline
from app.ai.intent_parser import parse_intent, parse_intent_voice, parse_split
from app.ai.schemas import AssistantIntent
from app.config import Settings
from app.database.session import SessionFactory
from app.debts.service import (
    DebtError,
    NothingToRepay,
    OverpaymentError,
    cancel_debt,
    cancel_payment,
    create_debt,
    debt_overview,
    match_person,
    outstanding_total,
    record_repayment,
    repay,
)
from app.models import AIProcessingLog, Debt, DebtPayment, PendingAction, Transaction
from app.models.enums import DebtDirection, TxDirection, TxSource, TxType
from app.transactions.categories import categories_prompt_text
from app.transactions.service import (
    TransactionNotFound,
    create_expense_for_debt,
    create_manual_transaction,
    purpose_text,
    soft_delete_transaction,
)
from app.transactions.splits import (
    PlannedLine,
    SplitError,
    apply_splits,
    describe_parts,
    plan_lines,
    precheck_debts,
    undo_explanation,
)
from app.utils.money import MAX_AMOUNT, candidate_amounts, format_money, to_decimal
from app.utils.text import escape_html
from app.utils.timeutil import utcnow

log = logging.getLogger(__name__)

PENDING_TTL = timedelta(hours=24)
VOICE_CONFIDENCE = 0.85


@dataclass
class AssistantReply:
    kind: str  # saved | confirm | clarify | unknown | error
    text: str
    pending_id: int | None = None
    options: list[tuple[str, str]] = field(default_factory=list)  # (label, value) choices for clarify
    undo: tuple[str, str] | None = None  # (entity kind, ref)
    transaction_id: int | None = None  # set when a bank payment was explained


def _payload_from_intent(intent: AssistantIntent, amount: Decimal, currency: str, when: datetime) -> dict:
    return {
        "intent": intent.intent,
        "person_name": intent.person_name,
        "amount": str(amount),
        "currency": currency,
        "direction": intent.direction,
        "description": intent.description,
        "category": intent.category,
        "subcategory": intent.subcategory,
        "item": intent.item,
        "occurred_at": when.isoformat(),
    }


def describe(payload: dict) -> str:
    amount = format_money(Decimal(payload["amount"]), payload["currency"])
    who = escape_html(payload.get("person_name") or "?")
    intent = payload["intent"]
    if intent == "create_debt":
        if payload.get("direction") == DebtDirection.I_OWE_THEM:
            return f"🔴 New debt: <b>you owe {who}</b> {amount}"
        if payload.get("direction") == DebtDirection.THEY_OWE_ME:
            return f"🟢 New debt: <b>{who} owes you</b> {amount}"
        return f"❔ New debt with {who}: {amount} (direction unknown)"
    if intent == "repay_debt":
        if payload.get("direction") == DebtDirection.I_OWE_THEM:
            return f"↩️ Repayment: <b>you paid {who} back</b> {amount}"
        if payload.get("direction") == DebtDirection.THEY_OWE_ME:
            return f"↩️ Repayment: <b>{who} paid you back</b> {amount}"
        return f"❔ Repayment with {who}: {amount} (who paid whom?)"
    cat = " › ".join(x for x in (payload.get("category"), payload.get("subcategory"), payload.get("item")) if x) or "Other"
    desc = f" — {escape_html(payload['description'])}" if payload.get("description") else ""
    if intent == "add_expense":
        return f"💸 Expense {amount} · {escape_html(cat)}{desc}"
    return f"💰 Income {amount} · {escape_html(cat)}{desc}"


DIRECTION_OPTIONS = {
    "create_debt": [("🔴 I owe them", DebtDirection.I_OWE_THEM.value), ("🟢 They owe me", DebtDirection.THEY_OWE_ME.value)],
    "repay_debt": [("I paid them back", DebtDirection.I_OWE_THEM.value), ("They paid me back", DebtDirection.THEY_OWE_ME.value)],
}

HELP_TEXT = (
    "I can record debts, repayments, expenses and income. Examples:\n"
    "• <i>19 000 Orzubekdan qarz bo'ldim</i>\n"
    "• <i>Orzubek mendan 20 ming qarzi bo'ldi</i>\n"
    "• <i>Ali 50 ming qarzini qaytardi</i>\n"
    "• <i>Bugun taksiga 25 ming sarfladim</i>\n"
    "• <i>Kecha 200 ming so'm maosh tushdi</i>\n"
    "You can also send a voice message."
)


def _quota_text(exc: AIQuotaExhausted) -> str:
    hours = max(0, (exc.retry_at - utcnow().timestamp()) / 3600)
    when = f"in about {hours:.0f} h" if hours >= 1 else "within the hour"
    return f"Today's free Gemini limit is used up (resets {when})."


class Assistant:
    def __init__(self, session_factory: SessionFactory, ai: AIClient, settings: Settings):
        self.sf = session_factory
        self.ai = ai
        self.settings = settings

    async def _log(self, source_type: str, status: str, raw: str | None, error: str | None) -> None:
        try:
            async with self.sf.begin() as s:
                s.add(AIProcessingLog(source_type=source_type, model=getattr(self.ai, "model", "?"),
                                      response=(raw or "")[:20000] or None, status=status, error=error))
        except Exception:
            log.exception("failed to write AI log")

    # ------------------------------------------------------------------ entry points
    async def handle_voice(self, audio: bytes, mime_type: str, *, idempotency_key: str, tz: ZoneInfo,
                           now: datetime | None = None) -> tuple[str | None, AssistantReply]:
        """One AI request transcribes AND understands the voice note, then the same validation as text."""
        now = now or utcnow()
        try:
            intent, raw = await parse_intent_voice(self.ai, audio, mime_type, now, tz)
        except AIQuotaExhausted as exc:
            await self._log("voice", "error", None, str(exc))
            return None, AssistantReply("error", f"⚠️ {_quota_text(exc)} Please type the message instead — I can still understand typed text.")
        except (AIUnavailable, AIInvalidResponse) as exc:
            await self._log("voice", "error", getattr(exc, "raw", None), str(exc))
            return None, AssistantReply("error", "⚠️ I couldn't understand the voice message right now. Please try again or type it.")
        await self._log("voice", "ok", raw, None)
        transcript = (intent.transcript or "").strip()
        reply = await self._validate_and_route(intent, transcript, TxSource.BOT_VOICE, idempotency_key, tz, now)
        return transcript or None, reply

    async def handle_text(self, text: str, *, source: TxSource, idempotency_key: str, tz: ZoneInfo,
                          now: datetime | None = None) -> AssistantReply:
        now = now or utcnow()
        text = (text or "").strip()[:1000]
        if not text:
            return AssistantReply("unknown", HELP_TEXT)
        try:
            intent, raw = await parse_intent(self.ai, text, now, tz)
        except (AIUnavailable, AIInvalidResponse) as exc:
            await self._log("assistant", "error", getattr(exc, "raw", None), str(exc))
            offline = parse_offline(text)
            if offline is None:
                why = _quota_text(exc) if isinstance(exc, AIQuotaExhausted) else "The AI assistant is unavailable right now."
                return AssistantReply("error", f"⚠️ {why} I couldn't understand this without it, nothing was saved.\n\n" + HELP_TEXT)
            reply = await self._validate_and_route(offline, text, source, idempotency_key, tz, now)
            reply.text = "🧩 <i>Understood without AI — please check.</i>\n" + reply.text
            return reply
        await self._log("assistant", "ok", raw, None)
        return await self._validate_and_route(intent, text, source, idempotency_key, tz, now)

    # ------------------------------------------------------------------ validation
    async def _validate_and_route(self, intent: AssistantIntent, text: str, source: TxSource,
                                  key: str, tz: ZoneInfo, now: datetime) -> AssistantReply:
        if intent.intent == "unknown":
            return AssistantReply("unknown", "🤔 I didn't recognise a financial operation.\n\n" + HELP_TEXT)

        if intent.amount is None or intent.amount <= 0 or intent.amount > MAX_AMOUNT:
            q = intent.clarification_question or "What is the amount?"
            return AssistantReply("clarify", f"❔ I couldn't determine the amount. {escape_html(q)}\nPlease resend the full message with the amount.")
        amount = to_decimal(intent.amount)
        currency = (intent.currency or self.settings.currency).upper()

        reasons: list[str] = []
        if amount not in candidate_amounts(text):
            reasons.append("the amount was interpreted, please double-check it")
        when = intent.transaction_datetime or now
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        if when > now + timedelta(days=1) or when < now - timedelta(days=366):
            reasons.append("unusual date")
            when = now if when > now + timedelta(days=1) else when
        if intent.intent in ("create_debt", "repay_debt") and not intent.person_name:
            return AssistantReply("clarify", "❔ Who is this debt with? Please resend the message with the person's name.")
        threshold = VOICE_CONFIDENCE if source == TxSource.BOT_VOICE else self.settings.ai_confidence_threshold
        if intent.needs_confirmation or intent.confidence < threshold:
            reasons.append("I'm not fully sure I understood")

        payload = _payload_from_intent(intent, amount, currency, when)

        if intent.intent in ("create_debt", "repay_debt") and intent.direction is None:
            pending = await self._create_pending(payload, source)
            question = intent.clarification_question or "Who owes whom?"
            return AssistantReply(
                "clarify",
                f"{describe(payload)}\n\n❔ {escape_html(question)}",
                pending_id=pending,
                options=DIRECTION_OPTIONS[intent.intent],
            )

        if reasons:
            pending = await self._create_pending(payload, source)
            return AssistantReply(
                "confirm",
                f"{describe(payload)}\n\n<i>Please confirm: {escape_html('; '.join(reasons))}.</i>",
                pending_id=pending,
            )
        async with self.sf.begin() as s:
            return await self._execute(s, payload, source, key, tz)

    async def _create_pending(self, payload: dict, source: TxSource) -> int:
        async with self.sf.begin() as s:
            p = PendingAction(kind=payload["intent"], payload=payload, source=source, expires_at=utcnow() + PENDING_TTL)
            s.add(p)
            await s.flush()
            return p.id

    # ------------------------------------------------------------------ confirmation
    async def confirm(self, pending_id: int, *, tz: ZoneInfo, direction: str | None = None) -> AssistantReply:
        """Execute a pending action exactly once (double taps are harmless)."""
        async with self.sf.begin() as s:
            res = await s.execute(
                update(PendingAction)
                .where(PendingAction.id == pending_id, PendingAction.status == "pending", PendingAction.expires_at > utcnow())
                .values(status="confirmed", updated_at=utcnow())
                .returning(PendingAction.payload, PendingAction.source)
            )
            row = res.first()
            if row is None:
                return AssistantReply("error", "This request has already been handled or has expired.")
            payload, source = dict(row[0]), row[1]
            if direction is not None:
                if direction not in (DebtDirection.I_OWE_THEM, DebtDirection.THEY_OWE_ME):
                    return AssistantReply("error", "Invalid choice.")
                payload["direction"] = direction
            if payload["intent"] in ("create_debt", "repay_debt") and not payload.get("direction"):
                return AssistantReply("error", "The debt direction is still unknown; nothing was saved.")
            try:
                async with s.begin_nested():
                    return await self._execute(s, payload, TxSource(source), f"pending:{pending_id}", tz)
            except DebtError as exc:
                return AssistantReply("error", f"⚠️ {escape_html(str(exc))}. Nothing was saved.")

    async def cancel(self, pending_id: int) -> bool:
        async with self.sf.begin() as s:
            res = await s.execute(
                update(PendingAction).where(PendingAction.id == pending_id, PendingAction.status == "pending")
                .values(status="cancelled", updated_at=utcnow())
            )
            return res.rowcount == 1

    # ------------------------------------------------------------------ execution
    async def _execute(self, s, payload: dict, source: TxSource, key: str, tz: ZoneInfo) -> AssistantReply:
        intent = payload["intent"]
        if intent == "explain":
            return await self._apply_explanation(s, payload, source, key)
        amount = Decimal(payload["amount"])
        currency = payload["currency"]
        when = datetime.fromisoformat(payload["occurred_at"])
        summary = describe(payload)

        if intent == "create_debt":
            debt, created = await create_debt(
                s, person_name=payload["person_name"], direction=DebtDirection(payload["direction"]), amount=amount,
                currency=currency, description=payload.get("description"), occurred_at=when, source=source,
                idempotency_key=key, category=payload.get("category"), subcategory=payload.get("subcategory"),
                item=payload.get("item"),
            )
            status = "✅ Saved" if created else "ℹ️ Already saved"
            purpose = " › ".join(x for x in (payload.get("category"), payload.get("subcategory"), payload.get("item")) if x)
            note = (f"\n🧾 For: {escape_html(purpose)} — counted as spending when you repay it"
                    if purpose and payload["direction"] == DebtDirection.I_OWE_THEM else
                    (f"\n🧾 For: {escape_html(purpose)}" if purpose else ""))
            return AssistantReply("saved", f"{status}\n{summary}{note}\n\n{await self._person_balance_line(s, debt.person_id, currency)}",
                                  undo=("debt", str(debt.id)))

        if intent == "repay_debt":
            person = await match_person(s, payload["person_name"])
            if person is None:
                return AssistantReply("error", f"⚠️ I don't have any debts with {escape_html(payload['person_name'])}. Nothing was saved.")
            direction = DebtDirection(payload["direction"])
            owed = await outstanding_total(s, person.id, direction, currency)
            if amount > owed and not payload.get("allow_excess"):
                extra = amount - owed
                who_now = f"{escape_html(person.name)} will owe you" if direction == DebtDirection.I_OWE_THEM else f"you will owe {escape_html(person.name)}"
                pending = await self._create_pending({**payload, "allow_excess": True}, source)
                return AssistantReply("confirm", f"{summary}\n\n<i>Only {format_money(owed, currency)} was owed — "
                                                  f"{who_now} the extra {format_money(extra, currency)}. Confirm?</i>",
                                      pending_id=pending)
            result = await repay(
                s, person_id=person.id, direction=direction, amount=amount, currency=currency, paid_at=when,
                description=payload.get("description"), idempotency_key=key, allow_excess=True, source=source,
            )
            lines = []
            for i, (debt, part) in enumerate(result.allocations):
                if direction == DebtDirection.I_OWE_THEM and debt.category_id:
                    # Paying back for something bought for me: this is when it becomes my spending.
                    await create_expense_for_debt(s, debt, part, when, source, f"{key}:p{i}", person.name)
                    lines.append(f"🧾 {format_money(part, currency)} → {escape_html(await purpose_text(s, debt))} (spending)")
            if result.excess_debt is not None:
                lines.append(f"➕ Extra {format_money(result.excess, currency)} recorded as a new debt")
            extra_text = ("\n" + "\n".join(lines)) if lines else ""
            return AssistantReply("saved", f"✅ Saved\n{summary}{extra_text}\n\n{await self._person_balance_line(s, person.id, currency)}",
                                  undo=("repayment", key))

        tx_type = TxType.EXPENSE if intent == "add_expense" else TxType.INCOME
        tx, created = await create_manual_transaction(
            s, transaction_type=tx_type, amount=amount, currency=currency, occurred_at=when,
            description=payload.get("description"), category=payload.get("category"),
            subcategory=payload.get("subcategory"), source=source, idempotency_key=key,
            item=payload.get("item"),
        )
        status = "✅ Saved" if created else "ℹ️ Already saved"
        return AssistantReply("saved", f"{status}\n{summary}\n<i>{when.astimezone(tz):%d.%m.%Y %H:%M}</i>", undo=("tx", str(tx.id)))

    async def _person_balance_line(self, s, person_id: int, currency: str) -> str:
        overview = await debt_overview(s, include_settled=True)
        for p in overview.people:
            if p.person_id == person_id and p.currency == currency:
                if p.net > 0:
                    return f"Balance with {escape_html(p.name)}: they owe you {format_money(p.net, currency)}"
                if p.net < 0:
                    return f"Balance with {escape_html(p.name)}: you owe {format_money(-p.net, currency)}"
                return f"Balance with {escape_html(p.name)}: settled ✅"
        return ""

    async def undo(self, kind: str, ref: str) -> str:
        async with self.sf.begin() as s:
            try:
                if kind == "debt":
                    await cancel_debt(s, int(ref), reason="undo from bot", actor="owner:bot")
                elif kind == "tx":
                    await soft_delete_transaction(s, int(ref), actor="owner:bot", reason="undo from bot")
                elif kind == "explain":
                    await undo_explanation(s, int(ref), actor="owner:bot")
                elif kind == "repayment":
                    payments = (await s.scalars(select(DebtPayment).where(DebtPayment.idempotency_key == ref))).all()
                    excess = (await s.scalars(select(Debt).where(Debt.idempotency_key == f"{ref}:excess"))).first()
                    if not payments and not excess:
                        return "Nothing to undo."
                    for p in payments:
                        await cancel_payment(s, p.id, reason="undo from bot", actor="owner:bot")
                    if excess:
                        await cancel_debt(s, excess.id, reason="undo from bot", actor="owner:bot")
                    spent = (await s.scalars(select(Transaction).where(
                        Transaction.idempotency_key.like(f"{ref}:p%"), Transaction.deleted_at.is_(None)))).all()
                    for t in spent:
                        await soft_delete_transaction(s, t.id, actor="owner:bot", reason="repayment undone")
                else:
                    return "Unknown item."
            except (DebtError, TransactionNotFound, SplitError, ValueError) as exc:
                return f"Could not undo: {escape_html(str(exc))}"
        return "↩️ Undone (kept in the audit history)."

    # ------------------------------------------------------------------ "what was this money for?"
    async def _explain_context(self, s, tx: Transaction, tz: ZoneInfo) -> tuple[str, str, str]:
        moved = "LEFT the owner's card" if tx.direction == TxDirection.OUT else "ARRIVED on the owner's card"
        facts = (
            f"amount: {tx.amount} {tx.currency} (money {moved})\n"
            f"counterparty as written by the bank: {tx.merchant or tx.operation_title or 'unknown'}\n"
            f"time: {tx.transaction_datetime.astimezone(tz):%d.%m.%Y %H:%M}"
        )
        overview = await debt_overview(s)
        people = "\n".join(
            f"- {p.name}: " + ("owes the owner " if p.net > 0 else "the owner owes ") + f"{abs(p.net)} {p.currency}"
            for p in overview.people if p.net != 0
        )
        return facts, await categories_prompt_text(s), people

    async def explain_transaction(self, tx_id: int, *, text: str | None = None, audio: tuple[bytes, str] | None = None,
                                  source: TxSource, idempotency_key: str, tz: ZoneInfo) -> tuple[str | None, AssistantReply]:
        """The owner's reply to "what was this for?" — text or voice, possibly several parts and debts."""
        async with self.sf() as s:
            tx = await s.get(Transaction, tx_id)
            if tx is None or tx.deleted_at is not None:
                return None, AssistantReply("error", "This transaction no longer exists.")
            facts, cats, people = await self._explain_context(s, tx, tz)
        try:
            answer, raw = await parse_split(self.ai, tx_facts=facts, categories=cats, people=people,
                                            text=(text or "")[:1000] if audio is None else None, audio=audio)
        except AIQuotaExhausted as exc:
            await self._log("explain", "error", None, str(exc))
            return None, AssistantReply("error", f"⚠️ {_quota_text(exc)} Reply again later — the payment stays as it is.")
        except (AIUnavailable, AIInvalidResponse) as exc:
            await self._log("explain", "error", getattr(exc, "raw", None), str(exc))
            return None, AssistantReply("error", "⚠️ I couldn't understand that right now. Please reply again a bit later.")
        await self._log("explain", "ok", raw, None)

        heard = (answer.transcript or "").strip() if audio is not None else (text or "")
        try:
            lines, problems = plan_lines(answer.lines, tx.amount, tx.direction)
        except SplitError as exc:
            q = f"\n❔ {escape_html(answer.clarification_question)}" if answer.clarification_question else ""
            return heard or None, AssistantReply("error", f"⚠️ {escape_html(str(exc))}. Nothing was changed — reply again to this question.{q}")

        # Stated amounts must really appear in what the owner said.
        mentioned = candidate_amounts(heard)
        if any(ln.stated and not ln.estimated and ln.amount not in mentioned for ln in lines) and len(lines) > 1:
            problems.append("please check the amounts")
        async with self.sf() as s:
            debt_errors, debt_notes = await precheck_debts(s, tx, lines)
        problems += debt_notes
        if debt_errors:
            return heard or None, AssistantReply("error", "⚠️ " + "; ".join(escape_html(e) for e in debt_errors)
                                                 + ". Nothing was changed — reply again to this question.")

        threshold = VOICE_CONFIDENCE if audio is not None else self.settings.ai_confidence_threshold
        payload = {"intent": "explain", "transaction_id": tx.id, "lines": [ln.to_json() for ln in lines]}
        if problems or answer.needs_confirmation or answer.confidence < threshold:
            pending = await self._create_pending(payload, source)
            body = self._explain_text(tx, lines)
            note = "; ".join(problems) or "I'm not fully sure I understood"
            return heard or None, AssistantReply("confirm", f"{body}\n\n<i>Please confirm: {escape_html(note)}.</i>", pending_id=pending)
        async with self.sf.begin() as s:
            return heard or None, await self._apply_explanation(s, payload, source, idempotency_key)

    async def quick_explain(self, tx_id: int, choice: str) -> AssistantReply:
        """One-tap answers from the question's buttons."""
        mapping = {
            "food": ("expense", "Food", None), "groceries": ("expense", "Shopping", "Groceries"),
            "taxi": ("expense", "Transport", "Taxi"), "shopping": ("expense", "Shopping", None),
            "bills": ("expense", "Bills and Utilities", None), "transfer": ("transfer", None, None),
            "income": ("income", "Income", None),
        }
        if choice not in mapping:
            return AssistantReply("error", "Unknown choice.")
        kind, cat, sub = mapping[choice]
        async with self.sf.begin() as s:
            tx = await s.get(Transaction, tx_id)
            if tx is None or tx.deleted_at is not None:
                return AssistantReply("error", "This transaction no longer exists.")
            if kind == "expense" and tx.direction != TxDirection.OUT:
                kind, cat, sub = "income", "Income", None
            line = PlannedLine(kind, tx.amount, cat, sub, stated=False)
            payload = {"intent": "explain", "transaction_id": tx.id, "lines": [line.to_json()]}
            return await self._apply_explanation(s, payload, TxSource.BOT_TEXT, f"quick:{tx.id}:{choice}:{utcnow().timestamp()}")

    async def _apply_explanation(self, s, payload: dict, source: TxSource, key: str) -> AssistantReply:
        lines = [PlannedLine.from_json(d) for d in payload["lines"]]
        try:
            async with s.begin_nested():
                tx = await apply_splits(s, payload["transaction_id"], lines, actor="owner:bot", key=key, source=source)
        except (SplitError, DebtError) as exc:
            return AssistantReply("error", f"⚠️ {escape_html(str(exc))}. Nothing was changed.")
        text = "✅ Saved\n" + self._explain_text(tx, lines)
        for ln in lines:
            if ln.kind in ("debt_repay", "debt_new"):
                person = await match_person(s, ln.person_name)
                if person:
                    text += "\n" + await self._person_balance_line(s, person.id, tx.currency)
        return AssistantReply("saved", text, undo=("explain", str(tx.id)), transaction_id=tx.id)

    @staticmethod
    def _explain_text(tx: Transaction, lines) -> str:
        sign = "−" if tx.direction == TxDirection.OUT else "+"
        head = f"{sign}{format_money(tx.amount, tx.currency)} · {escape_html(tx.merchant or 'payment')}"
        return head + "\n" + "\n".join(describe_parts(lines, tx.direction, tx.currency))
