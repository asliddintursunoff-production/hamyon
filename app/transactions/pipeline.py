"""HUMO message processing pipeline.

received -> parsing -> (financial record saved from parser facts) -> awaiting_ai -> classified -> saved
                                                                                         \\-> needs_review
Failures never lose the original message or the financial record; retries are idempotent.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.ai.classifier import classify_transaction
from app.ai.gemini import AIClient, AIInvalidResponse, AIQuotaExhausted, AIUnavailable
from app.config import Settings
from app.database.session import SessionFactory
from app.models import AIProcessingLog, Category, TelegramMessage, Transaction
from app.models.enums import ClassificationStatus, MsgKind, MsgStatus, SnapshotSource, TxType
from app.cards.service import ensure_card, record_balance_snapshot
from app.transactions.categories import categories_prompt_text, resolve_classification
from app.transactions.parser import (
    CardBalance,
    ParseError,
    ParsedTransaction,
    detect_kind,
    parse_balance_list,
    parse_transaction,
)
from app.transactions.rules import decide, find_merchant, looks_like_generic_payment, remember_ai_result
from app.transactions.service import Classification, apply_classification, create_humo_transaction
from app.utils.timeutil import utcnow

log = logging.getLogger(__name__)

LEASE = timedelta(minutes=10)
FRESH_FOR_NOTIFICATION = timedelta(minutes=30)
# PARSING/CLASSIFIED are only claimable after their lease (next_attempt_at) expired, i.e. a worker died.
CLAIMABLE = (MsgStatus.RECEIVED, MsgStatus.PARSING, MsgStatus.AWAITING_AI, MsgStatus.CLASSIFIED)


class Notifier(Protocol):
    async def transaction_processed(self, tx_id: int) -> None: ...


@dataclass
class IngestResult:
    row_id: int
    kind: MsgKind
    created: bool


@dataclass
class ProcessOutcome:
    status: MsgStatus
    transaction_id: int | None = None
    balances: list[CardBalance] | None = None


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 60 * 2 ** max(0, attempts - 1)))


class MessageProcessor:
    def __init__(
        self,
        session_factory: SessionFactory,
        ai: AIClient,
        settings: Settings,
        owner_id: int,
        notifier: Notifier | None = None,
    ):
        self.sf = session_factory
        self.ai = ai
        self.settings = settings
        self.owner_id = owner_id
        self.notifier = notifier

    # ---------------------------------------------------------------- ingestion
    async def ingest(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender_id: int | None,
        text: str,
        date: datetime,
        edit_date: datetime | None = None,
    ) -> IngestResult:
        """Persist the original message exactly once (unique chat_id + message_id)."""
        kind = detect_kind(text)
        async with self.sf.begin() as s:
            res = await s.execute(
                insert(TelegramMessage)
                .values(
                    chat_id=chat_id,
                    telegram_message_id=message_id,
                    sender_id=sender_id,
                    message_text=text,
                    message_date=date,
                    edit_date=edit_date,
                    kind=kind,
                    processing_status=MsgStatus.RECEIVED,
                )
                .on_conflict_do_nothing(index_elements=["chat_id", "telegram_message_id"])
                .returning(TelegramMessage.id)
            )
            row_id = res.scalar_one_or_none()
            if row_id is not None:
                return IngestResult(row_id, kind, True)

            existing = (
                await s.scalars(
                    select(TelegramMessage)
                    .where(TelegramMessage.chat_id == chat_id, TelegramMessage.telegram_message_id == message_id)
                    .with_for_update()
                )
            ).one()
            # Balance lists may be refreshed by editing the same message: re-process those edits.
            # Edits of transaction notifications are ignored so financial records never change silently.
            if (
                edit_date is not None
                and kind == MsgKind.BALANCE
                and existing.kind == MsgKind.BALANCE
                and (existing.edit_date is None or edit_date > existing.edit_date)
            ):
                existing.message_text = text
                existing.edit_date = edit_date
                existing.processing_status = MsgStatus.RECEIVED
                existing.next_attempt_at = None
                existing.attempts = 0
                return IngestResult(existing.id, kind, True)
            return IngestResult(existing.id, MsgKind(existing.kind), False)

    # ---------------------------------------------------------------- state helpers
    async def _claim(self, row_id: int) -> TelegramMessage | None:
        """Atomically lease a message so no other worker processes it concurrently."""
        now = utcnow()
        async with self.sf.begin() as s:
            res = await s.execute(
                update(TelegramMessage)
                .where(
                    TelegramMessage.id == row_id,
                    TelegramMessage.processing_status.in_(list(CLAIMABLE)),
                    or_(TelegramMessage.next_attempt_at.is_(None), TelegramMessage.next_attempt_at <= now),
                )
                .values(processing_status=MsgStatus.PARSING, next_attempt_at=now + LEASE, updated_at=now)
                .returning(TelegramMessage)
            )
            return res.scalars().first()

    async def _set_status(
        self,
        row_id: int,
        status: MsgStatus,
        *,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        attempts: int | None = None,
    ) -> None:
        values = dict(processing_status=status, processing_error=error, next_attempt_at=next_attempt_at, updated_at=utcnow())
        if attempts is not None:
            values["attempts"] = attempts
        async with self.sf.begin() as s:
            await s.execute(update(TelegramMessage).where(TelegramMessage.id == row_id).values(**values))

    async def _log_ai(self, source_type: str, source_id: int | None, status: str, raw: str | None, error: str | None) -> None:
        try:
            async with self.sf.begin() as s:
                s.add(
                    AIProcessingLog(
                        source_type=source_type,
                        source_id=source_id,
                        model=getattr(self.ai, "model", "unknown"),
                        response=(raw or "")[:20000] or None,
                        status=status,
                        error=(error or "")[:2000] or None,
                    )
                )
        except Exception:  # logging must never break processing
            log.exception("failed to write AI log")

    async def due_message_ids(self, limit: int = 100) -> list[int]:
        now = utcnow()
        async with self.sf() as s:
            rows = await s.scalars(
                select(TelegramMessage.id)
                .where(
                    TelegramMessage.processing_status.in_(list(CLAIMABLE)),
                    or_(TelegramMessage.next_attempt_at.is_(None), TelegramMessage.next_attempt_at <= now),
                )
                .order_by(TelegramMessage.id)
                .limit(limit)
            )
            return list(rows.all())

    # ---------------------------------------------------------------- processing
    async def process(self, row_id: int, *, sync_request_id: int | None = None) -> ProcessOutcome | None:
        msg = await self._claim(row_id)
        if msg is None:
            return None  # already processed or leased by another worker
        try:
            if msg.kind == MsgKind.BALANCE:
                return await self._process_balance(msg, sync_request_id)
            if msg.kind == MsgKind.TRANSACTION:
                return await self._process_transaction(msg)
            await self._set_status(msg.id, MsgStatus.IGNORED)
            return ProcessOutcome(MsgStatus.IGNORED)
        except ParseError as exc:
            log.warning("message %s could not be parsed: %s", msg.id, exc)
            await self._set_status(msg.id, MsgStatus.FAILED, error=f"parse error: {exc}")
            return ProcessOutcome(MsgStatus.FAILED)
        except Exception as exc:
            attempts = msg.attempts + 1
            log.exception("message %s processing failed (attempt %s)", msg.id, attempts)
            if attempts >= self.settings.ai_max_message_attempts:
                await self._set_status(msg.id, MsgStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000], attempts=attempts)
                return ProcessOutcome(MsgStatus.FAILED)
            await self._set_status(
                msg.id, MsgStatus.RECEIVED, error=f"{type(exc).__name__}"[:2000],
                next_attempt_at=utcnow() + _backoff(attempts), attempts=attempts,
            )
            return ProcessOutcome(MsgStatus.RECEIVED)

    async def _process_balance(self, msg: TelegramMessage, sync_request_id: int | None) -> ProcessOutcome:
        parsed = parse_balance_list(msg.message_text, self.settings.currency)
        recorded_at = msg.edit_date or msg.message_date
        source = SnapshotSource.BALANCE_REFRESH if sync_request_id else SnapshotSource.BALANCE_MESSAGE
        async with self.sf.begin() as s:
            for b in parsed.balances:
                card = await ensure_card(s, self.owner_id, b.card_last_four, bank_name=b.bank_name, currency=b.currency)
                await record_balance_snapshot(
                    s, card, balance=b.balance, currency=b.currency, recorded_at=recorded_at,
                    source=source, source_message_id=msg.id, sync_request_id=sync_request_id,
                )
            await s.execute(
                update(TelegramMessage)
                .where(TelegramMessage.id == msg.id)
                .values(processing_status=MsgStatus.SAVED, processing_error=None, next_attempt_at=None, updated_at=utcnow())
            )
        return ProcessOutcome(MsgStatus.SAVED, balances=parsed.balances)

    async def _process_transaction(self, msg: TelegramMessage) -> ProcessOutcome:
        tz = self.settings.tz
        parsed = parse_transaction(msg.message_text, tz, self.settings.currency)
        async with self.sf.begin() as s:
            tx = await create_humo_transaction(s, msg, parsed, self.owner_id)
            if tx is None:
                await s.execute(
                    update(TelegramMessage).where(TelegramMessage.id == msg.id)
                    .values(processing_status=MsgStatus.IGNORED, next_attempt_at=None,
                            processing_error="duplicate notification of an already recorded transaction")
                )
                return ProcessOutcome(MsgStatus.IGNORED)
            tx_id, user_edited, cstatus = tx.id, tx.user_edited, tx.classification_status
            if user_edited or cstatus not in (ClassificationStatus.PENDING, ClassificationStatus.FALLBACK):
                # Classified already: by a deterministic rule at creation (transfers, incoming money),
                # or a retry after a crash. No AI needed — but the owner may still need to be asked.
                await s.execute(
                    update(TelegramMessage).where(TelegramMessage.id == msg.id)
                    .values(processing_status=MsgStatus.SAVED, next_attempt_at=None, processing_error=None)
                )
                classified_early = True
            else:
                classified_early = False
        if classified_early:
            await self._notify(tx_id, msg.message_date)
            return ProcessOutcome(MsgStatus.SAVED, tx_id)

        async with self.sf.begin() as s:
            merchant = await find_merchant(s, parsed.merchant, parsed.direction)
            decision = decide(
                merchant, utcnow(), self.settings.merchant_negative_cache_days, self.settings.ai_confidence_threshold,
                generic=parsed.direction == "out" and looks_like_generic_payment(parsed.merchant),
            )
            if decision.kind == "use_rule":
                m = decision.merchant
                cat = await s.get(Category, m.category_id)
                sub = await s.get(Category, m.subcategory_id) if m.subcategory_id else None
                item = await s.get(Category, m.item_id) if m.item_id else None
                tx_type = m.transaction_type or tx.transaction_type
                await apply_classification(
                    s, tx_id,
                    Classification(
                        transaction_type=tx_type, category=cat, subcategory=sub,
                        is_spending=m.is_spending if m.is_spending is not None else tx_type == TxType.EXPENSE,
                        description=m.description, confidence=m.ai_confidence if not m.confirmed else Decimal("1"),
                        needs_review=False, status=ClassificationStatus.RULE, item=item,
                    ),
                )
                await s.execute(
                    update(TelegramMessage).where(TelegramMessage.id == msg.id)
                    .values(processing_status=MsgStatus.SAVED, next_attempt_at=None, processing_error=None)
                )
                status = MsgStatus.SAVED
            elif decision.kind == "known_unknown":
                await s.execute(
                    update(Transaction).where(Transaction.id == tx_id, Transaction.user_edited.is_(False))
                    .values(classification_status=ClassificationStatus.FALLBACK, needs_review=True)
                )
                await s.execute(
                    update(TelegramMessage).where(TelegramMessage.id == msg.id)
                    .values(processing_status=MsgStatus.NEEDS_REVIEW, next_attempt_at=None,
                            processing_error="merchant not identifiable (cached)")
                )
                status = MsgStatus.NEEDS_REVIEW
            else:
                status = None
                await s.execute(
                    update(TelegramMessage).where(TelegramMessage.id == msg.id)
                    .values(processing_status=MsgStatus.AWAITING_AI)
                )

        if status is None:
            status = await self._classify_with_ai(msg, tx_id, parsed)
        await self._notify(tx_id, msg.message_date)
        return ProcessOutcome(status, tx_id)

    async def _classify_with_ai(self, msg: TelegramMessage, tx_id: int, parsed: ParsedTransaction) -> MsgStatus:
        """Call Gemini *outside* any DB transaction, then validate and apply the result atomically."""
        async with self.sf() as s:
            categories_text = await categories_prompt_text(s)
        try:
            outcome = await classify_transaction(self.ai, msg.message_text, parsed, categories_text, self.settings.tz)
        except AIQuotaExhausted as exc:
            # Not the message's fault: wait for the quota reset without using up retry attempts.
            async with self.sf.begin() as s:
                await s.execute(
                    update(Transaction).where(Transaction.id == tx_id, Transaction.user_edited.is_(False))
                    .values(classification_status=ClassificationStatus.FALLBACK, needs_review=True)
                )
            retry_at = datetime.fromtimestamp(max(exc.retry_at, utcnow().timestamp() + 60), tz=utcnow().tzinfo)
            await self._set_status(msg.id, MsgStatus.AWAITING_AI, error="AI daily limit reached; waiting for reset",
                                   next_attempt_at=retry_at)
            return MsgStatus.AWAITING_AI
        except (AIUnavailable, AIInvalidResponse) as exc:
            raw = exc.raw if isinstance(exc, AIInvalidResponse) else None
            await self._log_ai("telegram_message", msg.id, "error", raw, str(exc))
            attempts = msg.attempts + 1
            async with self.sf.begin() as s:
                await s.execute(
                    update(Transaction).where(Transaction.id == tx_id, Transaction.user_edited.is_(False))
                    .values(classification_status=ClassificationStatus.FALLBACK, needs_review=True)
                )
            if attempts >= self.settings.ai_max_message_attempts:
                await self._set_status(msg.id, MsgStatus.NEEDS_REVIEW, error=f"AI failed: {exc}"[:2000], attempts=attempts)
                return MsgStatus.NEEDS_REVIEW
            await self._set_status(
                msg.id, MsgStatus.AWAITING_AI, error=f"AI failed: {exc}"[:2000],
                next_attempt_at=utcnow() + _backoff(attempts), attempts=attempts,
            )
            return MsgStatus.AWAITING_AI

        await self._log_ai("telegram_message", msg.id, "ok", outcome.raw, "; ".join(outcome.warnings) or None)
        await self._set_status(msg.id, MsgStatus.CLASSIFIED, next_attempt_at=utcnow() + LEASE)
        r = outcome.result
        confidence = Decimal(str(round(r.confidence, 3)))
        low_conf = r.confidence < self.settings.ai_confidence_threshold
        needs_review = bool(r.needs_review or low_conf or outcome.warnings)
        direction_conflict = any("money" in w for w in outcome.warnings)

        async with self.sf.begin() as s:
            tx = await s.get(Transaction, tx_id)
            tx_type = tx.transaction_type if direction_conflict else r.transaction_type
            category, subcategory = await resolve_classification(s, r.category, r.subcategory, tx_type)
            await apply_classification(
                s, tx_id,
                Classification(
                    transaction_type=tx_type, category=category, subcategory=subcategory,
                    is_spending=bool(r.is_spending and tx_type == TxType.EXPENSE),
                    description=r.description, confidence=confidence,
                    needs_review=needs_review, status=ClassificationStatus.AI,
                ),
            )
            if parsed.merchant:
                identified = not needs_review and category.normalized_name != "other"
                await remember_ai_result(
                    s, parsed.merchant,
                    direction=parsed.direction,
                    category_id=category.id if identified else None,
                    subcategory_id=subcategory.id if identified and subcategory else None,
                    transaction_type=tx_type if identified else None,
                    is_spending=(r.is_spending and tx_type == TxType.EXPENSE) if identified else None,
                    description=r.description if identified else None,
                    confidence=confidence if identified else None,
                    now=utcnow(),
                )
            final = MsgStatus.NEEDS_REVIEW if needs_review else MsgStatus.SAVED
            await s.execute(
                update(TelegramMessage).where(TelegramMessage.id == msg.id)
                .values(processing_status=final, next_attempt_at=None,
                        processing_error="; ".join(outcome.warnings) or None, updated_at=utcnow())
            )
        return final

    async def _notify(self, tx_id: int, message_date: datetime) -> None:
        if self.notifier is None:
            return
        async with self.sf.begin() as s:
            res = await s.execute(
                update(Transaction)
                .where(Transaction.id == tx_id, Transaction.notified_at.is_(None))
                .values(notified_at=utcnow())
                .returning(Transaction.id)
            )
            first_time = res.scalar_one_or_none() is not None
        # Don't flood the owner with notifications for old messages recovered by history sync.
        if first_time and utcnow() - message_date < FRESH_FOR_NOTIFICATION:
            try:
                await self.notifier.transaction_processed(tx_id)
            except Exception:
                log.exception("notification failed for transaction %s", tx_id)

    async def notify_recent(self) -> None:
        """After a restart: ask about fresh payments whose question was not sent yet."""
        if self.notifier is None:
            return
        async with self.sf() as s:
            rows = (await s.execute(
                select(Transaction.id, TelegramMessage.message_date)
                .join(TelegramMessage, TelegramMessage.id == Transaction.original_message_id)
                .where(Transaction.notified_at.is_(None), Transaction.deleted_at.is_(None),
                       TelegramMessage.message_date > utcnow() - FRESH_FOR_NOTIFICATION)
            )).all()
        for tx_id, date in rows:
            await self._notify(tx_id, date)

    async def reclassify(self, tx_id: int) -> bool:
        """Owner asked to retry AI for a transaction (e.g. after it ended in needs_review)."""
        async with self.sf.begin() as s:
            tx = await s.get(Transaction, tx_id)
            if tx is None or tx.original_message_id is None or tx.user_edited:
                return False
            tx.classification_status = ClassificationStatus.PENDING
            await s.execute(
                update(TelegramMessage).where(TelegramMessage.id == tx.original_message_id)
                .values(processing_status=MsgStatus.RECEIVED, attempts=0, next_attempt_at=None, processing_error=None)
            )
            if tx.merchant_id:
                from app.models import Merchant
                await s.execute(update(Merchant).where(Merchant.id == tx.merchant_id, Merchant.confirmed.is_(False))
                                .values(last_ai_at=None))
            return True

    async def reset_stale_leases(self) -> None:
        """Single instance: anything 'in progress' at startup was interrupted, make it retryable now."""
        async with self.sf.begin() as s:
            await s.execute(
                update(TelegramMessage)
                .where(TelegramMessage.processing_status.in_([MsgStatus.PARSING, MsgStatus.CLASSIFIED]))
                .values(processing_status=MsgStatus.RECEIVED, next_attempt_at=None)
            )


class WorkerPool:
    """Bounded in-process queue + workers + periodic sweeper. The DB is the source of truth, so a full
    queue or a crash only delays processing; the sweeper re-enqueues anything due."""

    def __init__(self, processor: MessageProcessor, workers: int = 2, maxsize: int = 1000, sweep_interval: float = 30.0):
        self.processor = processor
        self.queue: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        self.n = max(1, workers)
        self.sweep_interval = sweep_interval
        self._inflight: set[int] = set()
        self._tasks: list[asyncio.Task] = []

    def enqueue(self, row_id: int) -> bool:
        if row_id in self._inflight:
            return False
        try:
            self.queue.put_nowait(row_id)
        except asyncio.QueueFull:
            log.warning("processing queue full; message %s will be picked up by the sweeper", row_id)
            return False
        self._inflight.add(row_id)
        return True

    async def _worker(self) -> None:
        while True:
            row_id = await self.queue.get()
            try:
                await self.processor.process(row_id)
            except Exception:
                log.exception("worker failed on message %s", row_id)
            finally:
                self._inflight.discard(row_id)
                self.queue.task_done()

    async def _sweeper(self) -> None:
        while True:
            try:
                for row_id in await self.processor.due_message_ids():
                    self.enqueue(row_id)
            except Exception:
                log.exception("sweeper failed")
            await asyncio.sleep(self.sweep_interval)

    def start(self) -> None:
        from app.utils.tasks import supervised

        for i in range(self.n):
            self._tasks.append(supervised(f"pipeline-worker-{i}", self._worker))
        self._tasks.append(supervised("pipeline-sweeper", self._sweeper))

    async def stop(self) -> None:
        from app.utils.tasks import cancel_tasks

        await cancel_tasks(self._tasks)
        self._tasks.clear()
