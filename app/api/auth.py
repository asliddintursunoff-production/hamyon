"""Dashboard authentication: password login and one-time login links issued by the bot."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy import delete, update

from app.config import Settings
from app.database.session import SessionFactory
from app.models import DashboardLoginToken
from app.utils.timeutil import utcnow

SESSION_KEY = "owner"
LOGIN_LINK_TTL = timedelta(minutes=10)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_login_token(sf: SessionFactory) -> str:
    token = secrets.token_urlsafe(32)
    async with sf.begin() as s:
        await s.execute(delete(DashboardLoginToken).where(DashboardLoginToken.expires_at < utcnow()))
        s.add(DashboardLoginToken(token_hash=_hash(token), expires_at=utcnow() + LOGIN_LINK_TTL))
    return token


async def consume_login_token(sf: SessionFactory, token: str) -> bool:
    if not token or len(token) > 200:
        return False
    async with sf.begin() as s:
        res = await s.execute(
            update(DashboardLoginToken)
            .where(
                DashboardLoginToken.token_hash == _hash(token),
                DashboardLoginToken.used_at.is_(None),
                DashboardLoginToken.expires_at > utcnow(),
            )
            .values(used_at=utcnow())
        )
        return res.rowcount == 1


def password_ok(settings: Settings, password: str) -> bool:
    """The dashboard uses the same access code as the Telegram bot."""
    if not settings.bot_access_code or not settings.bot_access_code.get_secret_value().strip():
        return False
    expected = settings.bot_access_code.get_secret_value().strip()
    password = (password or "").strip()
    return hmac.compare_digest(hashlib.sha256((password or "").encode()).digest(), hashlib.sha256(expected.encode()).digest())


class LoginRateLimiter:
    """Simple in-process limiter (single instance app): max N failures per window."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 900):
        self.max = max_failures
        self.window = window_seconds
        self.failures: deque[float] = deque()

    def _trim(self) -> None:
        cutoff = time.monotonic() - self.window
        while self.failures and self.failures[0] < cutoff:
            self.failures.popleft()

    def blocked(self) -> bool:
        self._trim()
        return len(self.failures) >= self.max

    def fail(self) -> None:
        self.failures.append(time.monotonic())


def is_logged_in(request: Request) -> bool:
    return request.session.get(SESSION_KEY) is True


INIT_DATA_MAX_AGE = 24 * 3600


def validate_init_data(init_data: str, bot_token: str, *, now: float | None = None) -> int | None:
    """Verify Telegram Mini App initData (HMAC-SHA256 with the bot token). Returns the Telegram user id."""
    if not init_data or len(init_data) > 4096 or not bot_token:
        return None
    from urllib.parse import parse_qsl
    import json

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user_id = int(json.loads(pairs.get("user", "{}"))["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now or time.time()) - auth_date > INIT_DATA_MAX_AGE:
        return None
    return user_id


async def _telegram_user_ok(request: Request) -> bool:
    init_data = request.headers.get("x-telegram-init-data")
    if not init_data:
        return False
    settings = request.app.state.settings
    token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
    user_id = validate_init_data(init_data, token)
    # Same rule as the bot: the user must have unlocked it with the access code.
    return user_id is not None and await request.app.state.bot_auth.is_authenticated(user_id)


async def require_owner(request: Request) -> None:
    if is_logged_in(request) or await _telegram_user_ok(request):
        return
    raise HTTPException(status_code=401, detail="authentication required")


async def require_owner_write(request: Request) -> None:
    """Mutations need a custom header (Telegram initData or the dashboard's CSRF header)."""
    await require_owner(request)
    if request.headers.get("x-requested-with") != "finance-dashboard" and not request.headers.get("x-telegram-init-data"):
        raise HTTPException(status_code=403, detail="missing CSRF header")
