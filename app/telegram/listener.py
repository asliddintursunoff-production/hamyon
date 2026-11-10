"""Turns HUMO bot messages (live, edited or recovered from history) into pipeline work."""
from __future__ import annotations

import logging

from app.models.enums import MsgKind
from app.telegram.balance_sync import BalanceSyncCoordinator
from app.transactions.parser import ParseError, parse_balance_list
from app.transactions.pipeline import MessageProcessor, WorkerPool

log = logging.getLogger(__name__)


class HumoMessageHandler:
    def __init__(self, processor: MessageProcessor, pool: WorkerPool, balance: BalanceSyncCoordinator, settings):
        self.processor = processor
        self.pool = pool
        self.balance = balance
        self.settings = settings
        self.humo_chat_id: int | None = None

    async def handle(self, msg, *, edited: bool = False) -> None:
        """Validate and persist one message from the HUMO chat. Never blocks on AI."""
        if self.humo_chat_id is None or msg.out:
            return
        if msg.sender_id != self.humo_chat_id:
            return  # only the HUMO bot itself is trusted as a source
        start = self.settings.history_start
        if start is not None and msg.date < start:
            return  # older than the configured collection start date
        text = msg.raw_text or ""
        if not text.strip() or len(text) > 4096:
            return

        result = await self.processor.ingest(
            chat_id=self.humo_chat_id,
            message_id=msg.id,
            sender_id=msg.sender_id,
            text=text,
            date=msg.date,
            edit_date=msg.edit_date if edited else None,
        )

        if result.kind == MsgKind.BALANCE:
            pending = self.balance.match(msg.id, msg.edit_date if edited else None)
            if result.created:
                # Balance messages are cheap (no AI): process inline so the refresh answer is fresh.
                await self.processor.process(result.row_id, sync_request_id=pending.db_id if pending else None)
            if pending is not None:
                try:
                    self.balance.resolve(pending, parse_balance_list(text, self.settings.currency).balances, msg.id)
                except ParseError:
                    log.warning("balance answer %s could not be parsed", msg.id)
        elif result.created:
            self.pool.enqueue(result.row_id)
