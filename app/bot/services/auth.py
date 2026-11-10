"""Second factor for the Telegram bot: a secret access code only the owner knows.

- Only OWNER_TELEGRAM_ID can even reach this step (checked by middleware).
- Codes are compared in constant time; only an HMAC fingerprint of the configured code is stored, so
  changing BOT_ACCESS_CODE immediately invalidates existing unlocked sessions.
- Repeated wrong codes lock the bot for BOT_LOCKOUT_MINUTES.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.database.session import SessionFactory
from app.models import BotAuthSession
from app.utils.timeutil import utcnow


@dataclass
class VerifyResult:
    ok: bool
    locked_until: datetime | None = None
    attempts_left: int | None = None
    not_configured: bool = False


class BotAuthService:
    def __init__(self, sf: SessionFactory, settings: Settings):
        self.sf = sf
        self.settings = settings

    @property
    def code_configured(self) -> bool:
        code = self.settings.bot_access_code
        return bool(code and code.get_secret_value().strip())

    def _fingerprint(self) -> str:
        key = b"bot-access-code"
        return hmac.new(key, self.settings.bot_access_code.get_secret_value().strip().encode(), hashlib.sha256).hexdigest()

    async def _row(self, s, user_id: int, lock: bool = False) -> BotAuthSession:
        await s.execute(insert(BotAuthSession).values(telegram_user_id=user_id, failed_attempts=0).on_conflict_do_nothing())
        stmt = select(BotAuthSession).where(BotAuthSession.telegram_user_id == user_id)
        if lock:
            stmt = stmt.with_for_update()
        return (await s.scalars(stmt)).one()

    async def is_authenticated(self, user_id: int) -> bool:
        if not self.code_configured:
            return False
        async with self.sf() as s:
            row = await s.get(BotAuthSession, user_id)
            return bool(
                row
                and row.authenticated_until
                and row.authenticated_until > utcnow()
                and row.code_fingerprint
                and hmac.compare_digest(row.code_fingerprint, self._fingerprint())
            )

    async def locked_until(self, user_id: int) -> datetime | None:
        async with self.sf() as s:
            row = await s.get(BotAuthSession, user_id)
            if row and row.locked_until and row.locked_until > utcnow():
                return row.locked_until
            return None

    async def verify(self, user_id: int, code: str) -> VerifyResult:
        if not self.code_configured:
            return VerifyResult(False, not_configured=True)
        now = utcnow()
        async with self.sf.begin() as s:
            row = await self._row(s, user_id, lock=True)
            if row.locked_until and row.locked_until > now:
                return VerifyResult(False, locked_until=row.locked_until)
            expected = self.settings.bot_access_code.get_secret_value().strip()
            given = (code or "").strip()
            ok = hmac.compare_digest(
                hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest()
            )
            if ok:
                row.failed_attempts = 0
                row.locked_until = None
                row.authenticated_until = now + timedelta(days=self.settings.bot_session_days)
                row.code_fingerprint = self._fingerprint()
                return VerifyResult(True)
            row.failed_attempts += 1
            if row.failed_attempts >= self.settings.bot_max_code_attempts:
                row.failed_attempts = 0
                row.locked_until = now + timedelta(minutes=self.settings.bot_lockout_minutes)
                row.authenticated_until = None
                return VerifyResult(False, locked_until=row.locked_until)
            return VerifyResult(False, attempts_left=self.settings.bot_max_code_attempts - row.failed_attempts)

    async def unlocked_user_ids(self) -> list[int]:
        """Everyone currently unlocked with the current code (they all receive notifications)."""
        if not self.code_configured:
            return []
        fp = self._fingerprint()
        async with self.sf() as s:
            rows = (await s.scalars(select(BotAuthSession).where(BotAuthSession.authenticated_until > utcnow()))).all()
        return [r.telegram_user_id for r in rows if r.code_fingerprint and hmac.compare_digest(r.code_fingerprint, fp)]

    async def lock(self, user_id: int) -> None:
        async with self.sf.begin() as s:
            row = await self._row(s, user_id, lock=True)
            row.authenticated_until = None
