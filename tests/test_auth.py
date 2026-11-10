"""Bot access code, owner-only middleware, and dashboard authentication."""
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.api.auth import create_login_token
from app.bot.middleware import PrivateChatOnlyMiddleware, RequireUnlockedMiddleware
from app.bot.services.auth import BotAuthService

OWNER = 111


async def test_access_code_unlocks(sf, settings, owner_id):
    auth = BotAuthService(sf, settings)
    assert not await auth.is_authenticated(OWNER)
    assert (await auth.verify(OWNER, " open-sesame-42 ")).ok
    assert await auth.is_authenticated(OWNER)
    await auth.lock(OWNER)
    assert not await auth.is_authenticated(OWNER)


async def test_wrong_codes_lock_out(sf, settings, owner_id):
    settings.bot_max_code_attempts = 3
    auth = BotAuthService(sf, settings)
    r1 = await auth.verify(OWNER, "nope")
    r2 = await auth.verify(OWNER, "nope2")
    assert not r1.ok and r1.attempts_left == 2 and r2.attempts_left == 1
    r3 = await auth.verify(OWNER, "nope3")
    assert r3.locked_until is not None
    # even the correct code is refused while locked
    r4 = await auth.verify(OWNER, "open-sesame-42")
    assert not r4.ok and r4.locked_until is not None
    assert not await auth.is_authenticated(OWNER)


async def test_changing_code_invalidates_sessions(sf, settings, owner_id):
    auth = BotAuthService(sf, settings)
    assert (await auth.verify(OWNER, "open-sesame-42")).ok
    settings.bot_access_code = SecretStr("a-new-code")
    assert not await auth.is_authenticated(OWNER)


async def test_unconfigured_code_fails_closed(sf, settings, owner_id):
    settings.bot_access_code = None
    auth = BotAuthService(sf, settings)
    r = await auth.verify(OWNER, "")
    assert not r.ok and r.not_configured
    assert not await auth.is_authenticated(OWNER)


class FakeMessage:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


@pytest.mark.parametrize(
    "user_id, is_bot, chat_type, allowed",
    [(OWNER, False, "private", True), (999, False, "private", True), (OWNER, False, "group", False), (5, True, "private", False)],
)
async def test_private_chat_only_middleware(user_id, is_bot, chat_type, allowed):
    called = []

    async def handler(event, data):
        called.append(True)

    msg = FakeMessage()
    event = SimpleNamespace(message=msg, callback_query=None)
    data = {"ctx": SimpleNamespace(), "event_from_user": SimpleNamespace(id=user_id, is_bot=is_bot),
            "event_chat": SimpleNamespace(type=chat_type)}
    await PrivateChatOnlyMiddleware()(handler, event, data)
    assert bool(called) is allowed


async def test_any_user_needs_the_code(sf, settings, owner_id):
    """A stranger reaches the bot but cannot use anything until they enter the access code."""
    auth = BotAuthService(sf, settings)
    assert not await auth.is_authenticated(999)
    assert not (await auth.verify(999, "guess")).ok
    assert (await auth.verify(999, "open-sesame-42")).ok
    assert await auth.is_authenticated(999)
    assert sorted(await auth.unlocked_user_ids()) == [999]


async def test_owner_is_built_in(settings):
    assert settings.owner_telegram_id == 1220127328


async def test_locked_owner_cannot_use_features(sf, settings, owner_id):
    from aiogram.types import Chat, Message, User
    from datetime import datetime

    ctx = SimpleNamespace(auth=BotAuthService(sf, settings))
    sent = []
    msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=OWNER, type="private"),
                  from_user=User(id=OWNER, is_bot=False, first_name="me"), text="💰 My Finances")
    object.__setattr__(msg, "answer", lambda text, **kw: _record(sent, text))

    called = []

    async def handler(e, d):
        called.append(True)

    data = {"ctx": ctx, "event_from_user": SimpleNamespace(id=OWNER)}
    await RequireUnlockedMiddleware()(handler, msg, data)
    assert not called and "locked" in sent[0]
    await ctx.auth.verify(OWNER, "open-sesame-42")
    await RequireUnlockedMiddleware()(handler, msg, data)
    assert called


async def _record(sent, text):
    sent.append(text)


# ---------------------------------------------------------------- dashboard
@pytest.fixture
def web(sf, settings, owner_id):
    from app.main import create_app

    app = create_app()
    app.state.settings = settings
    app.state.sf = sf
    app.state.bot_auth = BotAuthService(sf, settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_dashboard_requires_auth(web):
    async with web as c:
        assert (await c.get("/api/overview")).status_code == 401
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_password_login_and_csrf(web):
    async with web as c:
        assert (await c.post("/login", data={"password": "wrong"})).status_code == 401
        r = await c.post("/login", data={"password": "open-sesame-42"})  # same code as the bot
        assert r.status_code == 303
        ov = await c.get("/api/overview?period=month")
        assert ov.status_code == 200
        body = ov.json()
        assert body["summary"]["income"] == "0.00"  # money is serialized as exact strings
        assert len(body["cards"]) == 3
        # mutation without the CSRF header is refused
        assert (await c.patch("/api/transactions/1", json={})).status_code == 403
        assert "Content-Security-Policy" in ov.headers


async def test_one_time_login_link(web, sf):
    token = await create_login_token(sf)
    async with web as c:
        r = await c.get(f"/auth/link?token={token}")
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert (await c.get("/api/cards")).status_code == 200
    async with httpx.AsyncClient(transport=web._transport, base_url="http://testserver") as c2:
        r = await c2.get(f"/auth/link?token={token}")  # reused token
        assert r.headers["location"].startswith("/login")
        assert (await c2.get("/api/cards")).status_code == 401


# ---------------------------------------------------------------- Telegram Mini App
def make_init_data(user_id: int, token: str, auth_date: int | None = None) -> str:
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import urlencode

    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH", "user": json.dumps({"id": user_id, "first_name": "Me"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_validation():
    import time

    from app.api.auth import validate_init_data

    good = make_init_data(42, "123:TOKEN")
    assert validate_init_data(good, "123:TOKEN") == 42
    assert validate_init_data(good, "999:OTHER") is None                       # signed by another bot
    assert validate_init_data(good.replace("42", "43"), "123:TOKEN") is None   # tampered user
    old = make_init_data(42, "123:TOKEN", auth_date=int(time.time()) - 3 * 86400)
    assert validate_init_data(old, "123:TOKEN") is None                        # expired
    assert validate_init_data("", "123:TOKEN") is None


async def test_mini_app_requires_unlocked_user(sf, settings, owner_id):
    from app.main import create_app

    settings.telegram_bot_token = SecretStr("123:TOKEN")
    app = create_app()
    app.state.settings = settings
    app.state.sf = sf
    app.state.bot_auth = BotAuthService(sf, settings)
    headers = {"X-Telegram-Init-Data": make_init_data(555, "123:TOKEN")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        page = await c.get("/app")
        assert page.status_code == 200 and "frame-ancestors https://web.telegram.org" in page.headers["Content-Security-Policy"]
        assert (await c.get("/api/overview", headers=headers)).status_code == 401  # valid Telegram user, but locked
        await app.state.bot_auth.verify(555, "open-sesame-42")
        r = await c.get("/api/analytics?period=month", headers=headers)
        assert r.status_code == 200 and "income_by_category" in r.json()
        forged = {"X-Telegram-Init-Data": make_init_data(555, "999:EVIL")}
        assert (await c.get("/api/overview", headers=forged)).status_code == 401
