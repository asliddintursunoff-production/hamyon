"""On-demand HUMO balance refresh through the owner's Telegram account.

Correlation: before pressing the button we record the newest message id in the HUMO chat and the
time. Only a balance message with a *higher* id (or an edit made *after* the request started) can
satisfy the request, so an old balance message can never be mistaken for a fresh answer.
Concurrency: at most one refresh is in flight; simultaneous callers share its result.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import update

from app.config import Settings
from app.database.session import SessionFactory
from app.models import BalanceSyncRequest
from app.telegram.buttons import list_buttons
from app.transactions.parser import CardBalance
from app.utils.timeutil import utcnow

log = logging.getLogger(__name__)


REPLY_KEYBOARD_KINDS = {"ButtonTypeDefault", "KeyboardButton"}


class BalanceRefreshError(Exception):
    pass


@dataclass
class RefreshResult:
    request_id: int
    balances: list[CardBalance]
    completed_at: datetime


@dataclass
class _Pending:
    db_id: int
    marker_message_id: int
    started_at: datetime
    future: asyncio.Future


class BalanceSyncCoordinator:
    def __init__(self, session_factory: SessionFactory, settings: Settings):
        self.sf = session_factory
        self.settings = settings
        self.client = None
        self.entity = None
        self._current: _Pending | None = None
        self._inflight: asyncio.Task | None = None

    def attach(self, client, entity) -> None:
        self.client, self.entity = client, entity

    @property
    def available(self) -> bool:
        return self.client is not None and self.entity is not None and self.client.is_connected()

    # -- called by the listener for every HUMO balance message
    def match(self, message_id: int, edit_date: datetime | None) -> _Pending | None:
        p = self._current
        if p is None or p.future.done():
            return None
        if message_id > p.marker_message_id:
            return p
        if edit_date is not None and edit_date >= p.started_at:
            return p
        return None

    def resolve(self, pending: _Pending, balances: list[CardBalance], message_id: int) -> None:
        if not pending.future.done():
            pending.future.set_result((balances, message_id))

    # -- public entry point
    async def refresh(self, requested_via: str) -> RefreshResult:
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.create_task(self._do_refresh(requested_via), name="balance-refresh")
        return await asyncio.shield(self._inflight)

    async def _find_button(self):
        exact = (self.settings.humo_balance_button_text or "").strip()
        pattern = re.compile(self.settings.humo_balance_button_pattern)
        for button, info in await list_buttons(self.client, self.entity):
            # Only plain menu-keyboard buttons. Inline buttons under HUMO messages are SETTINGS toggles
            # (e.g. the "💰" under a transaction hides/shows balances in notifications) — never press them.
            if info.kind not in REPLY_KEYBOARD_KINDS or not info.text:
                continue
            if (exact and info.text == exact) or (not exact and pattern.search(info.text)):
                return button, info
        return None, None

    async def _set_request(self, db_id: int, *, only_pending: bool = False, **values) -> None:
        async with self.sf.begin() as s:
            stmt = update(BalanceSyncRequest).where(BalanceSyncRequest.id == db_id)
            if only_pending:
                stmt = stmt.where(BalanceSyncRequest.status == "pending")
            await s.execute(stmt.values(**values))

    async def _do_refresh(self, requested_via: str) -> RefreshResult:
        async with self.sf.begin() as s:
            req = BalanceSyncRequest(status="pending", requested_via=requested_via)
            s.add(req)
            await s.flush()
            db_id = req.id

        try:
            if not self.available:
                raise BalanceRefreshError("Telegram account is not connected")
            button, info = await self._find_button()
            latest = await self.client.get_messages(self.entity, limit=1)
            marker = latest[0].id if latest else 0
            pending = _Pending(db_id, marker, utcnow() - timedelta(seconds=2), asyncio.get_running_loop().create_future())
            self._current = pending
            await self._set_request(db_id, marker_message_id=marker)

            if button is not None:
                log.info("balance refresh: pressing HUMO button %r (%s)", info.text, info.kind)
                await button.click()
            elif self.settings.humo_balance_button_text:
                # Configured reply-keyboard text (keyboard not present in recent history).
                log.info("balance refresh: sending configured button text")
                await self.client.send_message(self.entity, self.settings.humo_balance_button_text.strip(), parse_mode=None)
            else:
                raise BalanceRefreshError(
                    "Could not find the HUMO balance button. Run `python -m app.telegram.inspect_humo` "
                    "and set HUMO_BALANCE_BUTTON_TEXT to the exact button text."
                )

            try:
                balances, message_id = await asyncio.wait_for(
                    pending.future, timeout=self.settings.balance_refresh_timeout_seconds
                )
            except asyncio.TimeoutError:
                await self._set_request(db_id, status="timeout", error="no answer from HUMO bot", completed_at=utcnow())
                raise BalanceRefreshError("HUMO bot did not answer in time. Last known balances are kept.")

            now = utcnow()
            await self._set_request(db_id, status="completed", response_message_id=message_id, completed_at=now)
            return RefreshResult(db_id, balances, now)
        except BalanceRefreshError as exc:
            await self._set_request(db_id, only_pending=True, status="failed", error=str(exc)[:1000], completed_at=utcnow())
            raise
        except Exception as exc:
            log.exception("balance refresh failed")
            await self._set_request(db_id, only_pending=True, status="failed", error=type(exc).__name__, completed_at=utcnow())
            raise BalanceRefreshError("Balance refresh failed. Last known balances are kept.") from exc
        finally:
            self._current = None
